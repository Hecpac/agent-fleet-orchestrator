/* OS boundary fixture. Never reads or prints real credentials. */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/sysctl.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;

static int connect_port(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in address = {.sin_family=AF_INET, .sin_port=htons(port)};
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
    int result = connect(fd, (struct sockaddr *)&address, sizeof(address));
    close(fd);
    return result == 0;
}

int main(int argc, char **argv) {
    if (argc == 3 && strcmp(argv[1], "daemon") == 0) {
        pid_t child = fork();
        if (child < 0) return 2;
        if (child > 0) return 0;
        if (setsid() < 0) _exit(3);
        child = fork();
        if (child < 0) _exit(4);
        if (child > 0) _exit(0);
        FILE *f = fopen(argv[2], "w");
        if (!f) _exit(5);
        fprintf(f, "%d", getpid()); fclose(f);
        close(0); close(1); close(2);
        for (;;) pause();
    }
    if (argc == 2 && strcmp(argv[1], "hold") == 0) {
        for (;;) pause();
    }
    if (argc != 8) return 6;
    int fd = open(argv[1], O_RDONLY), read_ok = fd >= 0;
    if (fd >= 0) close(fd);
    fd = open(argv[2], O_WRONLY|O_CREAT, 0600);
    int write_ok = fd >= 0;
    if (fd >= 0) close(fd);
    int allowed = connect_port(atoi(argv[3]));
    int denied = connect_port(atoi(argv[4]));
    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un local = {.sun_family=AF_UNIX};
    snprintf(local.sun_path, sizeof(local.sun_path), "%s", argv[5]);
    int unix_ok = connect(fd, (struct sockaddr *)&local, sizeof(local)) == 0;
    close(fd);
    int query[] = {CTL_KERN, KERN_PROCARGS2, atoi(argv[6])};
    char buffer[65536]; size_t size = sizeof(buffer);
    int procargs_ok = sysctl(query, 3, buffer, &size, NULL, 0) == 0;
    pid_t child;
    char *args[] = {argv[7], "-c", "exit 0", NULL};
    int shell_ok = posix_spawn(&child, argv[7], NULL, NULL, args, environ) == 0;
    if (shell_ok) waitpid(child, NULL, 0);
    /* Fork is permitted, but its filesystem access must inherit the policy. */
    child = fork();
    if (child == 0) {
        fd = open(argv[1], O_RDONLY);
        _exit(fd >= 0 ? 0 : 1);
    }
    int child_status = 2;
    if (child > 0) waitpid(child, &child_status, 0);
    printf("{\"read\":%d,\"write\":%d,\"allowed_port\":%d,"
        "\"denied_port\":%d,\"unix\":%d,\"parent_procargs\":%d,"
        "\"shell\":%d,\"fork_read\":%d}\n", read_ok, write_ok, allowed,
        denied, unix_ok, procargs_ok, shell_ok,
        child > 0 && WIFEXITED(child_status) && WEXITSTATUS(child_status) == 0);
    return 0;
}
