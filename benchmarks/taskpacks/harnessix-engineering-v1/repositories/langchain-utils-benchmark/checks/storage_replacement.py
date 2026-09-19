from src.string_utils import sanitize_for_storage

assert sanitize_for_storage("a\x00b") == "ab"
assert sanitize_for_storage("a\x00b", " ") == "a b"
assert sanitize_for_storage("a\x00b", "[NUL]") == "a[NUL]b"
