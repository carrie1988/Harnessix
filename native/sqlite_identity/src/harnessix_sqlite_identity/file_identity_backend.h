/* 仅用于已求证引擎的独占启动期；嵌入式 SDK 不能假设该前提成立。
 * SQLite unix 的 syscall 表是引擎全局变量，必须在线程启动前一次装配。
 * 常驻回调只访问 C 状态；不得引用可被 Python module_free 清理的对象。 */
#include <sys/stat.h>
#include <string.h>
#include <errno.h>

typedef struct {
    unsigned calls;
    int status;
    struct stat observed;
} IdentityCapture;

static _Thread_local IdentityCapture *identity_capture;
static int (*original_fstat)(int, struct stat *);
static sqlite3_vfs *identity_vfs;
static const sqlite3_api_routines *identity_api;
static sqlite3_syscall_ptr (*identity_get_call)(sqlite3_vfs *, const char *);
static int (*identity_set_call)(sqlite3_vfs *, const char *, sqlite3_syscall_ptr);
static void *identity_engine_base;
static atomic_bool identity_ready;
/* 启动状态由 GIL 保护；callback 常驻但不持有任何 Python 对象。 */
typedef enum {
    BOOTSTRAP_NOT_STARTED,
    BOOTSTRAP_LOADING,
    BOOTSTRAP_PERMANENT,
    BOOTSTRAP_READY,
    BOOTSTRAP_FAILED,
} BootstrapPhase;
static BootstrapPhase identity_phase;

static int transparent_fstat(int fd, struct stat *result) {
    int rc = original_fstat(fd, result);
    int saved_errno = errno;
    IdentityCapture *capture = identity_capture;
    if (capture != NULL) {
        capture->calls++;
        capture->status = rc;
        if (rc == 0) capture->observed = *result;
    }
    errno = saved_errno;
    return rc;
}

static bool from_engine(void *function, void *engine_base) {
    Dl_info info;
    return function != NULL && dladdr(function, &info) != 0 &&
           info.dli_fbase == engine_base;
}

/* 在扩展入口中求证原 API/VFS，不安装 callback，也不保存 bootstrap db。
 * 后继公开 load_extension 成功返回，才证明永久驻留请求已被引擎接受。 */
static bool qualify_identity_backend(const sqlite3_api_routines *api) {
    const char *source =
        "2024-04-15 13:34:05 8653b758870e6ef0c98d46b3ace27849054af85da891eb121e9aaa537f1e8355";
    if (api->sourceid == NULL || strcmp(api->sourceid(), source) != 0 ||
        api->vfs_find == NULL || api->xthreadsafe == NULL || !api->xthreadsafe()) return false;
    sqlite3_vfs *vfs = api->vfs_find("unix");
    Dl_info info;
    if (vfs == NULL || api->vfs_find(NULL) != vfs || vfs->iVersion < 3 ||
        vfs->xGetSystemCall == NULL || vfs->xSetSystemCall == NULL ||
        dladdr((void *)api->file_control, &info) == 0 ||
        !from_engine((void *)vfs->xGetSystemCall, info.dli_fbase) ||
        !from_engine((void *)vfs->xSetSystemCall, info.dli_fbase) ||
        !from_engine((void *)vfs->xOpen, info.dli_fbase)) return false;
    sqlite3_syscall_ptr saved = vfs->xGetSystemCall(vfs, "fstat");
    /* 不叠加未知拦截器；其他 ABI/平台应独立求证，不静默接受同名函数。 */
    if (saved != (sqlite3_syscall_ptr)fstat) return false;
    identity_vfs = vfs;
    identity_api = api;
    identity_get_call = vfs->xGetSystemCall;
    identity_set_call = vfs->xSetSystemCall;
    identity_engine_base = info.dli_fbase;
    original_fstat = (int (*)(int, struct stat *))saved;
    return true;
}

static bool identity_backend_intact(void) {
    return atomic_load(&identity_ready) && identity_vfs != NULL &&
           identity_vfs->xGetSystemCall == identity_get_call &&
           identity_vfs->xSetSystemCall == identity_set_call &&
           identity_get_call(identity_vfs, "fstat") == (sqlite3_syscall_ptr)transparent_fstat;
}

/* 调用方已持原 db mutex，并以 lease 保活。只观察实际 main FD，不 prepare/step。
 * TLS 只限定本次 fstat 捕获，不绑定连接，不充当线程初始化或授权证明。 */
static int observe_main_identity(const sqlite3_api_routines *api, sqlite3 *db,
                                 uint64_t device, uint64_t inode) {
    if (api != identity_api || !identity_backend_intact() || identity_capture != NULL)
        return SQLITE_MISUSE;
    sqlite3_file *file = NULL;
    sqlite3_vfs *vfs = NULL;
    int rc = api->file_control(db, "main", SQLITE_FCNTL_FILE_POINTER, &file);
    if (rc != SQLITE_OK) return rc;
    rc = api->file_control(db, "main", SQLITE_FCNTL_VFS_POINTER, &vfs);
    if (rc != SQLITE_OK) return rc;
    if (vfs != identity_vfs || file == NULL || file->pMethods == NULL ||
        file->pMethods->iVersion < 2 || file->pMethods->xShmMap == NULL ||
        !from_engine((void *)file->pMethods->xFileSize, identity_engine_base))
        return SQLITE_NOTFOUND;
    IdentityCapture capture = {0};
    sqlite3_int64 size = -1;
    identity_capture = &capture;
    rc = file->pMethods->xFileSize(file, &size);
    identity_capture = NULL;
    if (rc != SQLITE_OK) return rc;
    if (!identity_backend_intact() || capture.calls != 1 || capture.status != 0 ||
        !S_ISREG(capture.observed.st_mode)) return SQLITE_IOERR;
    return (uint64_t)capture.observed.st_dev == device &&
           (uint64_t)capture.observed.st_ino == inode ? SQLITE_OK : SQLITE_MISMATCH;
}
