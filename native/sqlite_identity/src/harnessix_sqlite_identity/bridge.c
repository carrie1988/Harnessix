#define _GNU_SOURCE
#define PY_SSIZE_T_CLEAN
/* 所有 SQLite 调用只经过原连接提供的 API 表，禁止隐式全局 API 宏。 */
#define SQLITE_CORE 1
#include <Python.h>
#include <sqlite3ext.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <pthread.h>
#include <dlfcn.h>

#ifdef Py_GIL_DISABLED
#error "Free-threaded CPython has not been qualified for SQLite identity"
#endif

#define DUMMY_NAME "harnessix_b7_lifetime"
#define EXPORT __attribute__((visibility("default")))

typedef struct ConnectionState ConnectionState;
struct ConnectionState {
    atomic_uint refs;
    atomic_bool revoked;
    sqlite3 *db;
    const sqlite3_api_routines *api;
    sqlite3_stmt *lease;
    uint64_t device, inode; /* 来自工厂先前固定的物理 pin，不在 attach 自行取样。 */
    ConnectionState *next;
};
typedef struct {
    PyObject_HEAD
    PyObject *conn;
    PyObject *error;
    ConnectionState *state;
    uint64_t owner;
} IdentityToken;
typedef struct {
    PyObject *connection_type;
    PyObject *path;
    PyObject *error;
    PyObject *backend_error;
    PyObject *identity_error;
} ModuleState;
typedef struct {
    ConnectionState *state;
    PyObject *original_conn; /* 仅用于绑定；SQLite 回调不读取 Python 对象。 */
    bool consumed;
    bool bootstrap;
    const char *native_failure; /* 只由本扩展写入；不解析外部异常文本。 */
} Pending;

static _Thread_local Pending *pending;
static _Thread_local uint64_t thread_cookie;
static atomic_uint_fast64_t next_cookie = 1;
static _Atomic(const sqlite3_api_routines *) accepted_api;
static pthread_mutex_t registry_mutex = PTHREAD_MUTEX_INITIALIZER;
static ConnectionState *registry;
static atomic_ulong states_created, states_freed, leases_live, userdata_live;
static atomic_ulong destroys, file_controls, registrations;
static PyTypeObject IdentityTokenType;
#include "file_identity_backend.h"

static uint64_t current_cookie(void) {
    if (thread_cookie == 0) {
        thread_cookie = atomic_fetch_add(&next_cookie, 1);
    }
    return thread_cookie;
}
static ConnectionState *state_new(void) {
    ConnectionState *s = calloc(1, sizeof(*s));
    if (s == NULL) return NULL;
    atomic_init(&s->refs, 1); /* IdentityToken 持有的引用。 */
    atomic_init(&s->revoked, false);
    pthread_mutex_lock(&registry_mutex);
    s->next = registry;
    registry = s;
    pthread_mutex_unlock(&registry_mutex);
    atomic_fetch_add(&states_created, 1);
    return s;
}
static void state_drop(ConnectionState *s) {
    if (atomic_fetch_sub(&s->refs, 1) != 1) return;
    pthread_mutex_lock(&registry_mutex);
    ConnectionState **slot = &registry;
    while (*slot != s) slot = &(*slot)->next;
    *slot = s->next;
    pthread_mutex_unlock(&registry_mutex);
    atomic_fetch_add(&states_freed, 1);
    free(s);
}
static bool reserve_db(ConnectionState *s, sqlite3 *db,
                       const sqlite3_api_routines *api) {
    bool available = true;
    pthread_mutex_lock(&registry_mutex);
    for (ConnectionState *it = registry; it != NULL; it = it->next) {
        if (it != s && it->db == db && it->api == api) available = false;
    }
    if (available) { s->db = db; s->api = api; }
    pthread_mutex_unlock(&registry_mutex);
    return available;
}
static bool api_accept(const sqlite3_api_routines *api) {
    const sqlite3_api_routines *expected = NULL;
    if (atomic_compare_exchange_strong(&accepted_api, &expected, api)) return true;
    return expected == api;
}
/* 不返回认证值，不读 userdata，不调用 Python。SQLite 默认结果为 NULL。 */
static void dummy(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
    (void)ctx; (void)argc; (void)argv;
}
/* xDestroy 只撤销和释放自己的引用，绝不 finalize lease 或递归释放 IdentityToken。 */
static void dummy_destroy(void *data) {
    ConnectionState *s = data;
    atomic_store(&s->revoked, true);
    atomic_fetch_sub(&userdata_live, 1);
    atomic_fetch_add(&destroys, 1);
    state_drop(s);
}
static int extension_error(const sqlite3_api_routines *api, char **err,
                           const char *message) {
    if (pending != NULL) {
        if (pending->native_failure == NULL) pending->native_failure = message;
        /* 这是入口应答，不是身份准入。Python 调用成功返回后必须先消费失败记录，
         * 不能因重入入口留下的记录覆盖 load_extension 后继传播的调用方原异常。 */
        return SQLITE_OK;
    }
    if (err != NULL && api != NULL && api->mprintf != NULL) {
        *err = api->mprintf("%s", message);
    }
    return SQLITE_ERROR;
}

/* 仅在公开加载正常返回后消费本扩展的失败记录；绝不覆盖已抛出的加载/回调异常。 */
static bool raise_recorded_failure(ModuleState *module, const Pending *request) {
    if (request->native_failure != NULL) {
        PyErr_SetString(request->bootstrap ? module->backend_error : module->identity_error,
                        request->native_failure);
        return true;
    }
    return false;
}

EXPORT int sqlite3_extension_init(sqlite3 *db, char **err,
                                  const sqlite3_api_routines *api) {
    if (api == NULL || api->libversion_number == NULL ||
        api->libversion_number() != 3045003) {
        return extension_error(api, err, "UNSUPPORTED: qualified SQLite 3.45.3 required");
    }
    if (!api_accept(api)) {
        return extension_error(api, err, "API_MISMATCH: different SQLite API table");
    }
    if (pending == NULL || pending->consumed || pending->original_conn == NULL) {
        return extension_error(api, err, "INVALID: no original Connection pending");
    }
    pending->consumed = true;
    if (pending->bootstrap) {
        if (identity_phase != BOOTSTRAP_LOADING || db == NULL || !qualify_identity_backend(api)) {
            return extension_error(api, err, "UNSUPPORTED: unqualified identity backend");
        }
        /* 先请求永久驻留；不能发布可能随关闭而被卸载的全引擎 callback。 */
        return SQLITE_OK_LOAD_PERMANENTLY;
    }
    ConnectionState *s = pending->state;
    if (db == NULL || !reserve_db(s, db, api)) {
        return extension_error(api, err, "INVALID: duplicate attachment prohibited");
    }
    if (api->db_mutex(db) == NULL) {
        return extension_error(api, err, "UNSUPPORTED: serialized original connection required");
    }
    /* 准备但从不 step；公开 statement 生命周期阻止 close_v2 立即销毁 db。 */
    int rc = api->prepare_v2(db, "SELECT 1", -1, &s->lease, NULL);
    if (s->lease != NULL) atomic_fetch_add(&leases_live, 1);
    if (rc != SQLITE_OK || s->lease == NULL) {
        return extension_error(api, err, "INVALID: lease preparation failed");
    }
    int moved = -1;
    atomic_fetch_add(&file_controls, 1);
    rc = api->file_control(db, "main", SQLITE_FCNTL_HAS_MOVED, &moved);
    if (rc != SQLITE_OK || moved != 0) {
        return extension_error(api, err, "UNSUPPORTED_OR_MOVED: HAS_MOVED rejected");
    }
    if (observe_main_identity(api, db, s->device, s->inode) != SQLITE_OK) {
        return extension_error(api, err, "IDENTITY: actual main file rejected");
    }
    atomic_fetch_add(&s->refs, 1); /* SQLite userdata 自己的引用。 */
    atomic_fetch_add(&userdata_live, 1);
    rc = api->create_function_v2(db, DUMMY_NAME, 0,
                                SQLITE_UTF8 | SQLITE_DIRECTONLY, s,
                                dummy, NULL, NULL, dummy_destroy);
    /* SQLite 也会在注册失败时调用 xDestroy，不能手工再释放 userdata。 */
    if (rc != SQLITE_OK) {
        return extension_error(api, err, "INVALID: Dummy registration failed");
    }
    atomic_fetch_add(&registrations, 1);
    return SQLITE_OK;
}

static int token_fail(IdentityToken *t, const char *message, bool revoke) {
    if (revoke && t->state != NULL) atomic_store(&t->state->revoked, true);
    PyErr_SetString(t->error, message);
    return -1;
}
static int connection_open(IdentityToken *t) {
    PyObject *value = PyObject_GetAttrString(t->conn, "in_transaction");
    if (value == NULL) {
        PyErr_Clear();
        return token_fail(t, "CLOSED_OR_INVALID: original Connection unavailable", true);
    }
    /* 属性值可以为 False；属性读取成功本身才是公开关闭状态检查。 */
    int valid = PyBool_Check(value);
    Py_DECREF(value);
    if (!valid) return token_fail(t, "INVALID: in_transaction is not bool", true);
    return 0;
}
static int token_clear(IdentityToken *t) {
    ConnectionState *s = t->state;
    t->state = NULL; /* 在可能触发 xDestroy 的 finalize 之前断开 IdentityToken。 */
    if (s != NULL) {
        atomic_store(&s->revoked, true);
        sqlite3_stmt *lease = s->lease;
        s->lease = NULL;
        if (lease != NULL) {
            atomic_fetch_sub(&leases_live, 1);
            /* 避免其他线程的 Python progress 回调等待 GIL 时产生锁反转。 */
            Py_BEGIN_ALLOW_THREADS
            (void)s->api->finalize(lease);
            Py_END_ALLOW_THREADS
        }
        state_drop(s);
    }
    Py_CLEAR(t->conn);
    return 0;
}
static int token_traverse(IdentityToken *t, visitproc visit, void *arg) {
    Py_VISIT(t->conn);
    Py_VISIT(t->error);
    return 0;
}
static void token_dealloc(IdentityToken *t) {
    PyObject_GC_UnTrack(t);
    (void)token_clear(t);
    Py_CLEAR(t->error);
    Py_TYPE(t)->tp_free((PyObject *)t);
}
/* errcode 的 zombie 拒绝行为由原 SQLite 3.45.3 源码和实际探针确认。
 * lease 保证 db 内存与 mutex 尚存；同一原 mutex 内检查后再做 file_control。
 * 不用 get_autocommit 比较，不执行 SQL，不覆盖调用方任何回调。 */
static int native_observe(ConnectionState *s, int *moved, int *file_rc) {
    sqlite3_mutex *mutex = s->api->db_mutex(s->db);
    s->api->mutex_enter(mutex);
    int code = s->api->errcode(s->db);
    if ((code & 0xff) != SQLITE_MISUSE && moved != NULL) {
        atomic_fetch_add(&file_controls, 1);
        *file_rc = s->api->file_control(s->db, "main", SQLITE_FCNTL_HAS_MOVED, moved);
        if (*file_rc == SQLITE_OK && *moved == 0) {
            *file_rc = observe_main_identity(s->api, s->db, s->device, s->inode);
        }
    }
    s->api->mutex_leave(mutex);
    return code;
}
static PyObject *token_check(IdentityToken *t, PyObject *unused) {
    (void)unused;
    if (t->owner != current_cookie()) {
        token_fail(t, "THREAD: original thread required", false);
        return NULL;
    }
    ConnectionState *s = t->state;
    if (s == NULL || atomic_load(&s->revoked)) {
        token_fail(t, "REVOKED_OR_RELEASED: IdentityToken unavailable", false);
        return NULL;
    }
    if (connection_open(t) < 0) return NULL;
    int moved = -1;
    int rc = SQLITE_MISUSE;
    int native_code;
    /* 进度回调内只重入原 SQLite 的非 SQL API 与递归 mutex。 */
    Py_BEGIN_ALLOW_THREADS
    native_code = native_observe(s, &moved, &rc);
    Py_END_ALLOW_THREADS
    if ((native_code & 0xff) == SQLITE_MISUSE) {
        token_fail(t, "NATIVE_CLOSED_OR_INVALID: original SQLite handle rejected", true);
        return NULL;
    }
    if (connection_open(t) < 0) return NULL;
    if (atomic_load(&s->revoked)) {
        token_fail(t, "REVOKED: Dummy destroyed during check", false);
        return NULL;
    }
    if (rc != SQLITE_OK || (moved != 0 && moved != 1)) {
        token_fail(t, "UNSUPPORTED_OR_INVALID: original main identity check failed", true);
        return NULL;
    }
    if (moved != 0) {
        token_fail(t, "MOVED: original SQLite file no longer matches pathname", true);
        return NULL;
    }
    Py_RETURN_TRUE;
}
static PyObject *token_release(IdentityToken *t, PyObject *unused) {
    (void)unused;
    if (t->state != NULL && t->owner != current_cookie()) {
        token_fail(t, "THREAD: original thread required for release", false);
        return NULL;
    }
    (void)token_clear(t);
    Py_RETURN_NONE;
}
static PyMethodDef token_methods[] = {
    {"check", (PyCFunction)token_check, METH_NOARGS, "非 SQL 检查原连接文件身份。"},
    {"release", (PyCFunction)token_release, METH_NOARGS, "幂等撤销并释放原生 lease。"},
    {NULL, NULL, 0, NULL}
};
static PyTypeObject IdentityTokenType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "harnessix_sqlite_identity._bridge.IdentityToken",
    .tp_basicsize = sizeof(IdentityToken),
    .tp_dealloc = (destructor)token_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC |
                Py_TPFLAGS_IMMUTABLETYPE | Py_TPFLAGS_DISALLOW_INSTANTIATION,
    .tp_traverse = (traverseproc)token_traverse,
    .tp_clear = (inquiry)token_clear,
    .tp_methods = token_methods,
    .tp_doc = "只由 attach_identity 返回的原连接、原线程原生令牌。"
};
static PyObject *attach_token(PyObject *module, PyObject *conn,
                              uint64_t device, uint64_t inode) {
    ModuleState *m = PyModule_GetState(module);
    if (Py_TYPE(conn) != (PyTypeObject *)m->connection_type) {
        PyErr_SetString(PyExc_TypeError, "exact sqlite3.Connection required");
        return NULL;
    }
    if (pending != NULL) {
        PyErr_SetString(m->identity_error, "INVALID: reentrant attachment prohibited");
        return NULL;
    }
    IdentityToken *t = (IdentityToken *)IdentityTokenType.tp_alloc(&IdentityTokenType, 0);
    if (t == NULL) return NULL;
    t->conn = Py_NewRef(conn);
    t->error = Py_NewRef(m->identity_error);
    t->owner = current_cookie();
    t->state = state_new();
    if (t->state == NULL) { Py_DECREF(t); return PyErr_NoMemory(); }
    if (connection_open(t) < 0) { Py_DECREF(t); return NULL; }
    t->state->device = device;
    t->state->inode = inode;
    Pending request = { .state = t->state, .original_conn = conn };
    pending = &request;
    /* 不打开新连接，不改 extension 授权策略；由调用方先显式启用加载。 */
    PyObject *result = PyObject_CallMethod(conn, "load_extension", "O", m->path);
    pending = NULL;
    if (result == NULL) {
        Py_DECREF(t);
        return NULL;
    }
    Py_DECREF(result);
    if (raise_recorded_failure(m, &request)) {
        Py_DECREF(t);
        return NULL;
    }
    if (!request.consumed || t->state->lease == NULL ||
        atomic_load(&t->state->revoked) || connection_open(t) < 0) {
        if (!PyErr_Occurred()) PyErr_SetString(m->identity_error, "INVALID: bridge not initialized");
        Py_DECREF(t);
        return NULL;
    }
    return (PyObject *)t;
}
static PyObject *attach_identity(PyObject *module, PyObject *args) {
    PyObject *conn, *device, *inode;
    if (!PyArg_ParseTuple(args, "OOO", &conn, &device, &inode)) return NULL;
    if (!PyLong_CheckExact(device) || !PyLong_CheckExact(inode)) {
        PyErr_SetString(PyExc_TypeError, "exact integer physical pin required");
        return NULL;
    }
    uint64_t dev = PyLong_AsUnsignedLongLong(device);
    if (PyErr_Occurred()) return NULL;
    uint64_t ino = PyLong_AsUnsignedLongLong(inode);
    if (PyErr_Occurred()) return NULL;
    if (!identity_backend_intact()) {
        ModuleState *m = PyModule_GetState(module);
        PyErr_SetString(m->backend_error, "UNAVAILABLE: intact explicit bootstrap required");
        return NULL;
    }
    return attach_token(module, conn, dev, ino);
}
static PyObject *initialize_backend(PyObject *module, PyObject *conn) {
    ModuleState *m = PyModule_GetState(module);
    if (Py_TYPE(conn) != (PyTypeObject *)m->connection_type) {
        PyErr_SetString(PyExc_TypeError, "exact sqlite3.Connection required");
        return NULL;
    }
    if (pending != NULL || (identity_phase != BOOTSTRAP_NOT_STARTED &&
                            identity_phase != BOOTSTRAP_READY)) {
        PyErr_SetString(m->backend_error, "INVALID: reentrant or failed bootstrap");
        return NULL;
    }
    if (identity_phase == BOOTSTRAP_READY) {
        if (identity_backend_intact()) Py_RETURN_NONE;
        PyErr_SetString(m->backend_error, "INVALID: identity backend changed");
        return NULL;
    }
    identity_phase = BOOTSTRAP_LOADING;
    Pending request = { .original_conn = conn, .bootstrap = true };
    pending = &request;
    PyObject *result = PyObject_CallMethod(conn, "load_extension", "O", m->path);
    pending = NULL;
    if (result == NULL) {
        identity_phase = BOOTSTRAP_FAILED;
        return NULL;
    }
    Py_DECREF(result);
    if (raise_recorded_failure(m, &request)) {
        identity_phase = BOOTSTRAP_FAILED;
        return NULL;
    }
    if (!request.consumed || identity_vfs == NULL || identity_api == NULL) {
        identity_phase = BOOTSTRAP_FAILED;
        PyErr_SetString(m->backend_error, "INVALID: bootstrap not consumed");
        return NULL;
    }
    /* 扩展已永久驻留；随后失败不得虚称无副作用或自行重试。 */
    identity_phase = BOOTSTRAP_PERMANENT;
    int rc = identity_set_call(identity_vfs, "fstat", (sqlite3_syscall_ptr)transparent_fstat);
    if (rc != SQLITE_OK || identity_get_call(identity_vfs, "fstat") !=
                           (sqlite3_syscall_ptr)transparent_fstat) {
        identity_phase = BOOTSTRAP_FAILED;
        PyErr_SetString(m->backend_error, "INVALID: permanent backend installation failed");
        return NULL;
    }
    atomic_store(&identity_ready, true);
    identity_phase = BOOTSTRAP_READY;
    Py_RETURN_NONE;
}

static PyObject *stats(PyObject *module, PyObject *unused) {
    (void)module; (void)unused;
    return Py_BuildValue("{s:k,s:k,s:k,s:k,s:k,s:k,s:k,s:k}",
        "states_created", atomic_load(&states_created),
        "states_freed", atomic_load(&states_freed),
        "states_live", atomic_load(&states_created) - atomic_load(&states_freed),
        "leases_live", atomic_load(&leases_live),
        "userdata_live", atomic_load(&userdata_live),
        "destroys", atomic_load(&destroys),
        "file_controls", atomic_load(&file_controls),
        "registrations", atomic_load(&registrations));
}
static PyMethodDef module_methods[] = {
    {"initialize_backend", initialize_backend, METH_O, "独占启动期永久装配原引擎身份观察。"},
    {"attach_identity", attach_identity, METH_VARARGS, "绑定原连接并比对工厂固定的 dev/ino。"},
    {"_resource_counts", stats, METH_NOARGS, "内部生命周期诊断；不能替代内存检查。"},
    {NULL, NULL, 0, NULL}
};
static int module_traverse(PyObject *module, visitproc visit, void *arg) {
    ModuleState *m = PyModule_GetState(module);
    Py_VISIT(m->connection_type); Py_VISIT(m->path); Py_VISIT(m->error);
    Py_VISIT(m->backend_error); Py_VISIT(m->identity_error);
    return 0;
}
static int module_clear(PyObject *module) {
    ModuleState *m = PyModule_GetState(module);
    Py_CLEAR(m->connection_type); Py_CLEAR(m->path); Py_CLEAR(m->error);
    Py_CLEAR(m->backend_error); Py_CLEAR(m->identity_error);
    return 0;
}
/* 引用计数路径可以直接 dealloc，不保证先调用 m_clear；两条路径必须都清引用。 */
static void module_free(void *module) {
    (void)module_clear((PyObject *)module);
}
static struct PyModuleDef definition = {
    PyModuleDef_HEAD_INIT,
    .m_name = "harnessix_sqlite_identity._bridge",
    .m_doc = "原 SQLite main 文件点时身份；不授予 Git 写权限。",
    .m_size = sizeof(ModuleState),
    .m_methods = module_methods,
    .m_traverse = module_traverse,
    .m_clear = module_clear,
    .m_free = module_free
};
PyMODINIT_FUNC PyInit__bridge(void) {
    if (PyInterpreterState_Get() != PyInterpreterState_Main()) {
        PyErr_SetString(PyExc_ImportError, "UNSUPPORTED: subinterpreters");
        return NULL;
    }
    if (PyType_Ready(&IdentityTokenType) < 0) return NULL;
    PyObject *module = PyModule_Create(&definition);
    if (module == NULL) return NULL;
    ModuleState *m = PyModule_GetState(module);
    PyObject *sqlite = PyImport_ImportModule("_sqlite3");
    if (sqlite != NULL) {
        m->connection_type = PyObject_GetAttrString(sqlite, "Connection");
        Py_DECREF(sqlite);
    }
    Dl_info info;
    char *path = NULL;
    if (dladdr((void *)PyInit__bridge, &info) != 0) {
        path = realpath(info.dli_fname, NULL);
    }
    if (path != NULL) { m->path = PyUnicode_DecodeFSDefault(path); free(path); }
    else if (!PyErr_Occurred()) PyErr_SetString(PyExc_ImportError, "library path unavailable");
    m->error = PyErr_NewException("harnessix_sqlite_identity._bridge.BridgeError", PyExc_RuntimeError, NULL);
    if (m->error != NULL) {
        m->backend_error = PyErr_NewException(
            "harnessix_sqlite_identity._bridge.BackendUnavailable", m->error, NULL);
        m->identity_error = PyErr_NewException(
            "harnessix_sqlite_identity._bridge.ConnectionIdentityError", m->error, NULL);
    }
    if (m->connection_type == NULL || m->path == NULL || m->error == NULL ||
        m->backend_error == NULL || m->identity_error == NULL ||
        PyModule_AddObjectRef(module, "IdentityToken", (PyObject *)&IdentityTokenType) < 0 ||
        PyModule_AddObjectRef(module, "BridgeError", m->error) < 0 ||
        PyModule_AddObjectRef(module, "BackendUnavailable", m->backend_error) < 0 ||
        PyModule_AddObjectRef(module, "ConnectionIdentityError", m->identity_error) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
