#!/usr/bin/env python3
"""Exercise the real code-memory mapper's reservation-allocation rollback."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = (root / "dlls/ntdll/unix/horizon.c").read_text()
start = source.index("int wine_nx_code_memory_map(")
end = source.index("\n}", start) + 2
function = source[start:end]

fixture = r"""
#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef unsigned int Handle;
typedef unsigned int Result;
typedef int BOOL;
typedef uint64_t u64;
typedef struct VirtmemReservation {
    void *address;
    size_t size;
} VirtmemReservation;
struct wine_nx_code_memory {
    unsigned int handle;
    void *rw, *rx;
    void *rw_token, *rx_token;
    size_t size;
};

#define INVALID_HANDLE 0
#define R_FAILED(rc) ((rc) != 0)
#define MAKERESULT(module, description) (0x80000000u | ((module) << 9) | (description))
enum { Module_Libnx = 345, LibnxError_OutOfMemory = 2 };
enum { CodeMapOperation_MapOwner = 1, CodeMapOperation_MapSlave = 2,
       CodeMapOperation_UnmapOwner = 3, CodeMapOperation_UnmapSlave = 4 };
enum { Perm_Rw = 3, Perm_Rx = 5 };
#define OOM_RESULT MAKERESULT(Module_Libnx, LibnxError_OutOfMemory)

static pthread_mutex_t mapping_mutex = PTHREAD_MUTEX_INITIALIZER;
static int virtmem_depth, reservation_calls, remove_calls, fail_reservation_at;
static int run_calls, missing_run_at, create_rc, owner_rc, slave_rc, close_calls;
static int operations[8], operation_count;
static Handle created_handle = 0x77;

static void reset(void)
{
    virtmem_depth = reservation_calls = remove_calls = fail_reservation_at = 0;
    run_calls = missing_run_at = create_rc = owner_rc = slave_rc = close_calls = 0;
    operation_count = 0;
    memset(operations, 0, sizeof(operations));
}

static void virtmemLock(void) { assert(!virtmem_depth); virtmem_depth = 1; }
static void virtmemUnlock(void) { assert(virtmem_depth == 1); virtmem_depth = 0; }

static VirtmemReservation *virtmemAddReservation(void *address, size_t size)
{
    VirtmemReservation *rv;
    assert(virtmem_depth == 1 && address && size);
    reservation_calls++;
    if (reservation_calls == fail_reservation_at) return NULL;
    rv = malloc(sizeof(*rv));
    assert(rv);
    rv->address = address;
    rv->size = size;
    return rv;
}

static void virtmemRemoveReservation(VirtmemReservation *rv)
{
    assert(virtmem_depth == 1 && rv);
    remove_calls++;
    free(rv);
}

static void *find_code_run_locked(size_t size)
{
    assert(virtmem_depth == 1 && size == 0x1000);
    run_calls++;
    if (run_calls == missing_run_at) return NULL;
    return (void *)(uintptr_t)(0x100000 + run_calls * 0x100000);
}

static Result svcCreateCodeMemory(Handle *handle, void *source, size_t size)
{
    assert(source == (void *)0x500000 && size == 0x1000);
    if (create_rc) return create_rc;
    *handle = created_handle;
    return 0;
}

static Result svcControlCodeMemory(Handle handle, int operation,
                                   void *address, size_t size, int permission)
{
    assert(handle == created_handle && address && size == 0x1000);
    if (operation == CodeMapOperation_MapOwner || operation == CodeMapOperation_MapSlave)
        assert(virtmem_depth == 1);
    else
        assert(operation == CodeMapOperation_UnmapOwner ||
               operation == CodeMapOperation_UnmapSlave);
    assert(operation_count < 8);
    operations[operation_count++] = operation;
    if (operation == CodeMapOperation_MapOwner) {
        assert(permission == Perm_Rw);
        return owner_rc;
    }
    if (operation == CodeMapOperation_MapSlave) {
        assert(permission == Perm_Rx);
        return slave_rc;
    }
    assert(operation == CodeMapOperation_UnmapOwner ||
           operation == CodeMapOperation_UnmapSlave);
    return 0;
}

static void svcCloseHandle(Handle handle)
{
    assert(!virtmem_depth && handle == created_handle);
    close_calls++;
}

""" + function + r"""

static void assert_zero(const struct wine_nx_code_memory *out)
{
    assert(!out->handle && !out->rw && !out->rx &&
           !out->rw_token && !out->rx_token && !out->size);
}

static void test_success(void)
{
    struct wine_nx_code_memory out;
    unsigned int rc = 99;
    reset();
    assert(wine_nx_code_memory_map((void *)0x500000, 0x1000, &out, &rc));
    assert(!rc && out.handle == created_handle && out.size == 0x1000);
    assert(out.rw == (void *)0x200000 && out.rx == (void *)0x300000);
    assert(out.rw_token && out.rx_token);
    assert(operation_count == 2 && operations[0] == CodeMapOperation_MapOwner &&
           operations[1] == CodeMapOperation_MapSlave);
    assert(reservation_calls == 2 && !remove_calls && !close_calls && !virtmem_depth);
    virtmemLock();
    virtmemRemoveReservation(out.rw_token);
    virtmemRemoveReservation(out.rx_token);
    virtmemUnlock();
}

static void test_rw_reservation_oom(void)
{
    struct wine_nx_code_memory out;
    unsigned int rc = 0;
    reset(); fail_reservation_at = 1;
    assert(!wine_nx_code_memory_map((void *)0x500000, 0x1000, &out, &rc));
    assert(rc == OOM_RESULT && reservation_calls == 1);
    assert(!operation_count && !remove_calls && close_calls == 1 && !virtmem_depth);
    assert_zero(&out);
}

static void test_rx_reservation_oom(void)
{
    struct wine_nx_code_memory out;
    unsigned int rc = 0;
    reset(); fail_reservation_at = 2;
    assert(!wine_nx_code_memory_map((void *)0x500000, 0x1000, &out, &rc));
    assert(rc == OOM_RESULT && reservation_calls == 2);
    assert(operation_count == 2 && operations[0] == CodeMapOperation_MapOwner &&
           operations[1] == CodeMapOperation_UnmapOwner);
    assert(remove_calls == 1 && close_calls == 1 && !virtmem_depth);
    assert_zero(&out);
}

static void test_owner_map_failure(void)
{
    struct wine_nx_code_memory out;
    unsigned int rc = 0;
    reset(); owner_rc = 0x1234;
    assert(!wine_nx_code_memory_map((void *)0x500000, 0x1000, &out, &rc));
    assert(rc == owner_rc && operation_count == 1 &&
           operations[0] == CodeMapOperation_MapOwner);
    assert(remove_calls == 1 && close_calls == 1 && !virtmem_depth);
    assert_zero(&out);
}

static void test_slave_map_failure(void)
{
    struct wine_nx_code_memory out;
    unsigned int rc = 0;
    reset(); slave_rc = 0x2345;
    assert(!wine_nx_code_memory_map((void *)0x500000, 0x1000, &out, &rc));
    assert(rc == slave_rc && operation_count == 3);
    assert(operations[0] == CodeMapOperation_MapOwner &&
           operations[1] == CodeMapOperation_MapSlave &&
           operations[2] == CodeMapOperation_UnmapOwner);
    assert(remove_calls == 2 && close_calls == 1 && !virtmem_depth);
    assert_zero(&out);
}

static void test_no_free_run(void)
{
    struct wine_nx_code_memory out;
    unsigned int rc = 99;
    reset(); missing_run_at = 1;
    assert(!wine_nx_code_memory_map((void *)0x500000, 0x1000, &out, &rc));
    assert(!rc && !reservation_calls && !operation_count && close_calls == 1);
    assert_zero(&out);
}

static void test_no_second_run(void)
{
    struct wine_nx_code_memory out;
    unsigned int rc = 99;
    reset(); missing_run_at = 2;
    assert(!wine_nx_code_memory_map((void *)0x500000, 0x1000, &out, &rc));
    assert(!rc && reservation_calls == 1 && operation_count == 2);
    assert(operations[0] == CodeMapOperation_MapOwner &&
           operations[1] == CodeMapOperation_UnmapOwner);
    assert(remove_calls == 1 && close_calls == 1);
    assert_zero(&out);
}

static void test_create_failure(void)
{
    struct wine_nx_code_memory out;
    unsigned int rc = 0;
    reset(); create_rc = 0x3456;
    assert(!wine_nx_code_memory_map((void *)0x500000, 0x1000, &out, &rc));
    assert(rc == create_rc && !reservation_calls && !operation_count && !close_calls);
    assert_zero(&out);
}

int main(void)
{
    test_success();
    test_rw_reservation_oom();
    test_rx_reservation_oom();
    test_owner_map_failure();
    test_slave_map_failure();
    test_no_free_run();
    test_no_second_run();
    test_create_failure();
    puts("Horizon code-memory reservation rollback tests passed");
    return 0;
}
"""

with tempfile.TemporaryDirectory(prefix="wine-nx-code-memory-") as tmp:
    path = Path(tmp) / "test.c"
    binary = Path(tmp) / "test"
    path.write_text(fixture)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-pthread", str(path), "-o", str(binary)],
                   check=True)
    subprocess.run([str(binary)], check=True)
