/*
 * sombra: the command-line shim inside Sombra.app (Contents/Helpers/sombra), ADR 0050.
 *
 * Users run `sombra` from a terminal through a symlink to this file. It starts the
 * bundle's real executable (Contents/MacOS/Sombra) with the responsibility "disclaim"
 * spawn attribute, so macOS treats Sombra itself, not the terminal, as the responsible
 * process: TCC then asks for and records Microphone, Screen Recording and Accessibility
 * under Sombra's bundle id. stdin/stdout/stderr, arguments and the environment pass
 * through unchanged; the shim waits and exits with the child's status (or re-raises the
 * signal that killed it).
 *
 * responsibility_spawnattrs_setdisclaim is private SPI in libSystem (LLDB, Chromium and
 * Qt Creator use it for the same reason). It is looked up at run time; when it is
 * missing the child is started normally and inherits the terminal's grants, which
 * `sombra doctor` reports.
 *
 * Builds on Linux too (without the disclaim), so its pass-through is unit-tested there.
 */
#define _GNU_SOURCE 1 /* RTLD_DEFAULT on glibc */
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif

extern char **environ;

/* Relative to this shim's real directory (Contents/Helpers). */
#define TARGET_RELPATH "/../MacOS/Sombra"

static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) {
        kill(child, sig);
    }
}

static int self_path(char *out) {
#ifdef __APPLE__
    char raw[PATH_MAX];
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0) {
        return -1;
    }
    return realpath(raw, out) ? 0 : -1;
#else
    return realpath("/proc/self/exe", out) ? 0 : -1;
#endif
}

typedef int (*disclaim_fn)(posix_spawnattr_t *, int);

int main(int argc, char **argv) {
    (void)argc;
    char self[PATH_MAX];
    char target[PATH_MAX];
    char resolved[PATH_MAX];

    if (self_path(self) != 0) {
        fprintf(stderr, "sombra: cannot find the shim's own path: %s\n", strerror(errno));
        return 127;
    }
    char *slash = strrchr(self, '/');
    if (slash == NULL) {
        fprintf(stderr, "sombra: unexpected shim path: %s\n", self);
        return 127;
    }
    *slash = '\0';
    if (snprintf(target, sizeof target, "%s%s", self, TARGET_RELPATH) >= (int)sizeof target ||
        realpath(target, resolved) == NULL) {
        fprintf(stderr, "sombra: Sombra.app is incomplete: %s%s is missing\n", self,
                TARGET_RELPATH);
        return 127;
    }

    posix_spawnattr_t attr;
    if (posix_spawnattr_init(&attr) != 0) {
        return 127;
    }
    /* The child gets default handlers and an empty mask, whatever the shim changes. */
    sigset_t all, none;
    sigfillset(&all);
    sigemptyset(&none);
    posix_spawnattr_setsigdefault(&attr, &all);
    posix_spawnattr_setsigmask(&attr, &none);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK);

    disclaim_fn disclaim = (disclaim_fn)dlsym(RTLD_DEFAULT,
                                              "responsibility_spawnattrs_setdisclaim");
    if (disclaim != NULL) {
        disclaim(&attr, 1);
    }

    /* Ctrl-C / Ctrl-\ already reach the child (same process group): don't die first.
     * Signals sent to the shim alone (kill, a closed terminal) are forwarded. */
    signal(SIGINT, SIG_IGN);
    signal(SIGQUIT, SIG_IGN);
    signal(SIGTERM, forward);
    signal(SIGHUP, forward);
    signal(SIGUSR1, forward);
    signal(SIGUSR2, forward);

    argv[0] = resolved;
    pid_t pid;
    int err = posix_spawn(&pid, resolved, NULL, &attr, argv, environ);
    posix_spawnattr_destroy(&attr);
    if (err != 0) {
        fprintf(stderr, "sombra: cannot start %s: %s\n", resolved, strerror(err));
        return 126;
    }
    child = pid;

    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            fprintf(stderr, "sombra: waitpid: %s\n", strerror(errno));
            return 127;
        }
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        int sig = WTERMSIG(status);
        signal(sig, SIG_DFL);
        sigset_t one;
        sigemptyset(&one);
        sigaddset(&one, sig);
        sigprocmask(SIG_UNBLOCK, &one, NULL);
        raise(sig);
        return 128 + sig;
    }
    return 127;
}
