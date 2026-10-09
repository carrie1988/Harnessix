"""显式 CAS 控制端口只运输本次异常，普通端口与真实坏数据不改写。"""

from __future__ import annotations

import asyncio

import pytest

from harnessix.agent.errors import KernelError
from harnessix.workspace.blob_read_control import (
    WorkspaceBlobReader,
    read_workspace_blob,
    workspace_blob_read_boundary,
)
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


@pytest.mark.parametrize("kind", ["blob", "nested", "cancel", "timeout", "value"])
def test_callback_first_object_crosses_data_mapping_without_extra_unwrap(kind):
    error = {
        "blob": KernelError("delivery_blob_corrupt", "original observation"),
        "nested": UpstreamCheckpointError(UpstreamCheckpointError(ValueError("original"))),
        "cancel": asyncio.CancelledError("original cancellation"),
        "timeout": TimeoutError("original deadline"),
        "value": ValueError("original observer"),
    }[kind]
    marks = []

    def controlled(digest, mark):
        assert digest == "original-digest"
        marked = mark(error)
        marks.append(marked)
        raise marked

    reader = WorkspaceBlobReader(lambda _: pytest.fail("must consume controlled port"), controlled)
    with pytest.raises(BaseException) as caught:
        with workspace_blob_read_boundary(reader) as read:
            try:
                read("original-digest")
            except KernelError:
                pytest.fail("observer must not enter bad-data mapping")
    assert caught.value is error and len(marks) == 1


@pytest.mark.parametrize("nested", [False, True])
def test_foreign_reader_marker_is_not_owned_by_consumer(nested):
    error = UpstreamCheckpointError(KernelError("delivery_blob_corrupt", "external marker"))
    if nested:
        error = UpstreamCheckpointError(error)

    def controlled(_digest, _mark):
        raise error

    reader = WorkspaceBlobReader(lambda _: b"unused", controlled)
    with pytest.raises(BaseException) as caught:
        with workspace_blob_read_boundary(reader) as read:
            read("original-digest")
    assert caught.value is error


def test_earlier_owned_marker_survives_later_caught_failure():
    first, second = TimeoutError("first"), ValueError("later")

    def controlled(digest, mark):
        raise mark(first if digest == "first" else second)

    reader = WorkspaceBlobReader(lambda _: b"unused", controlled)
    with pytest.raises(BaseException) as caught:
        with workspace_blob_read_boundary(reader) as read:
            try:
                read("first")
            except UpstreamCheckpointError as marked:
                try:
                    read("second")
                except UpstreamCheckpointError:
                    pass
                raise marked
    assert caught.value is first


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_reader_keeps_exact_plain_call_shape(kind):
    calls = []

    def original(digest):
        calls.append(digest)
        return b"original-body"

    class Proxy:
        def __call__(self, digest):
            return original(digest)

    class Subclass(WorkspaceBlobReader):
        pass

    reader = {
        "function": original,
        "proxy": Proxy(),
        "subclass": Subclass(original, lambda *_: pytest.fail("no implicit controlled dispatch")),
    }[kind]
    with workspace_blob_read_boundary(reader) as read:
        assert read is reader
        assert (
            read_workspace_blob(read, "original-digest", UpstreamCheckpointError)
            == b"original-body"
        )
    assert calls == ["original-digest"]


@pytest.mark.parametrize("code", ["delivery_blob_corrupt", "delivery_blob_invalid"])
def test_actual_cas_error_remains_available_to_original_data_mapping(code):
    error = KernelError(code, "physical CAS failure")

    def controlled(_digest, _mark):
        raise error

    reader = WorkspaceBlobReader(lambda _: b"unused", controlled)
    with workspace_blob_read_boundary(reader) as read:
        with pytest.raises(KernelError) as caught:
            read("original-digest")
    assert caught.value is error
