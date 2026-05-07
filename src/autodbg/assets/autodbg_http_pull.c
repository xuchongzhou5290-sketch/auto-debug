/*
 * Minimal HTTP pull helper for constrained embedded Linux targets.
 *
 * Build with the target toolchain, copy the resulting executable to the SD
 * card, then run it on the device:
 *
 *   /mnt/sdcard/autodbg/autodbg-http-pull http://host:8765 /mnt/sdcard/autodbg autodbg-files.txt
 *
 * The helper intentionally depends only on libc + POSIX sockets. It downloads
 * a newline-delimited file list first, then downloads each relative path into
 * the requested workspace. It only supports plain HTTP.
 */

#include <ctype.h>
#include <errno.h>
#include <netdb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define BUF_SIZE 4096
#define DEFAULT_HTTP_PORT "80"

struct http_base {
    char host[256];
    char port[16];
    char path[512];
};

static void trim_line(char *line) {
    size_t len = strlen(line);
    while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r' || isspace((unsigned char)line[len - 1]))) {
        line[--len] = '\0';
    }
}

static int starts_with(const char *text, const char *prefix) {
    return strncmp(text, prefix, strlen(prefix)) == 0;
}

static int parse_base_url(const char *url, struct http_base *base) {
    const char *cursor = url;
    const char *host_start;
    const char *host_end;
    const char *port_start = NULL;
    const char *path_start;
    size_t host_len;
    size_t port_len;

    memset(base, 0, sizeof(*base));
    strcpy(base->port, DEFAULT_HTTP_PORT);
    strcpy(base->path, "");

    if (starts_with(cursor, "http://")) {
        cursor += 7;
    } else {
        fprintf(stderr, "Only http:// URLs are supported: %s\n", url);
        return 1;
    }

    host_start = cursor;
    path_start = strchr(cursor, '/');
    if (path_start == NULL) {
        path_start = cursor + strlen(cursor);
    }
    host_end = path_start;
    for (cursor = host_start; cursor < path_start; cursor++) {
        if (*cursor == ':') {
            host_end = cursor;
            port_start = cursor + 1;
            break;
        }
    }

    host_len = (size_t)(host_end - host_start);
    if (host_len == 0 || host_len >= sizeof(base->host)) {
        fprintf(stderr, "Invalid HTTP host in URL: %s\n", url);
        return 1;
    }
    memcpy(base->host, host_start, host_len);
    base->host[host_len] = '\0';

    if (port_start != NULL) {
        port_len = (size_t)(path_start - port_start);
        if (port_len == 0 || port_len >= sizeof(base->port)) {
            fprintf(stderr, "Invalid HTTP port in URL: %s\n", url);
            return 1;
        }
        memcpy(base->port, port_start, port_len);
        base->port[port_len] = '\0';
    }

    if (*path_start != '\0') {
        if (strlen(path_start) >= sizeof(base->path)) {
            fprintf(stderr, "HTTP base path is too long.\n");
            return 1;
        }
        strcpy(base->path, path_start);
        if (strcmp(base->path, "/") == 0) {
            base->path[0] = '\0';
        }
    }
    return 0;
}

static int connect_http(const struct http_base *base) {
    struct addrinfo hints;
    struct addrinfo *result = NULL;
    struct addrinfo *item;
    int sock = -1;
    int rc;

    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    rc = getaddrinfo(base->host, base->port, &hints, &result);
    if (rc != 0) {
        fprintf(stderr, "getaddrinfo(%s:%s) failed: %s\n", base->host, base->port, gai_strerror(rc));
        return -1;
    }

    for (item = result; item != NULL; item = item->ai_next) {
        sock = socket(item->ai_family, item->ai_socktype, item->ai_protocol);
        if (sock < 0) {
            continue;
        }
        if (connect(sock, item->ai_addr, item->ai_addrlen) == 0) {
            break;
        }
        close(sock);
        sock = -1;
    }

    freeaddrinfo(result);
    return sock;
}

static int mkdir_p(const char *path) {
    char tmp[1024];
    char *cursor;

    if (strlen(path) >= sizeof(tmp)) {
        fprintf(stderr, "Path too long: %s\n", path);
        return 1;
    }
    strcpy(tmp, path);
    for (cursor = tmp + 1; *cursor; cursor++) {
        if (*cursor == '/') {
            *cursor = '\0';
            if (mkdir(tmp, 0755) != 0 && errno != EEXIST) {
                perror(tmp);
                return 1;
            }
            *cursor = '/';
        }
    }
    if (mkdir(tmp, 0755) != 0 && errno != EEXIST) {
        perror(tmp);
        return 1;
    }
    return 0;
}

static int mkdir_parent(const char *path) {
    char tmp[1024];
    char *slash;

    if (strlen(path) >= sizeof(tmp)) {
        fprintf(stderr, "Path too long: %s\n", path);
        return 1;
    }
    strcpy(tmp, path);
    slash = strrchr(tmp, '/');
    if (slash == NULL) {
        return 0;
    }
    *slash = '\0';
    if (tmp[0] == '\0') {
        return 0;
    }
    return mkdir_p(tmp);
}

static int safe_join(char *out, size_t out_size, const char *workspace, const char *relative_path) {
    if (relative_path[0] == '/' || strstr(relative_path, "..") != NULL) {
        fprintf(stderr, "Unsafe relative path: %s\n", relative_path);
        return 1;
    }
    if (snprintf(out, out_size, "%s/%s", workspace, relative_path) >= (int)out_size) {
        fprintf(stderr, "Output path too long: %s/%s\n", workspace, relative_path);
        return 1;
    }
    return 0;
}

static int http_get_to_file(const struct http_base *base, const char *relative_path, const char *output_path) {
    char request[1200];
    char url_path[1024];
    char buffer[BUF_SIZE];
    char header[8192];
    size_t header_len = 0;
    int header_done = 0;
    int sock;
    FILE *out = NULL;
    ssize_t count;
    int status = 0;

    if (base->path[0] != '\0') {
        if (snprintf(url_path, sizeof(url_path), "%s/%s", base->path, relative_path) >= (int)sizeof(url_path)) {
            fprintf(stderr, "URL path too long: %s/%s\n", base->path, relative_path);
            return 1;
        }
    } else if (snprintf(url_path, sizeof(url_path), "/%s", relative_path) >= (int)sizeof(url_path)) {
        fprintf(stderr, "URL path too long: /%s\n", relative_path);
        return 1;
    }

    sock = connect_http(base);
    if (sock < 0) {
        fprintf(stderr, "Failed to connect to %s:%s\n", base->host, base->port);
        return 1;
    }

    if (snprintf(
            request,
            sizeof(request),
            "GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\nUser-Agent: autodbg-http-pull/1\r\n\r\n",
            url_path,
            base->host) >= (int)sizeof(request)) {
        fprintf(stderr, "HTTP request too long.\n");
        close(sock);
        return 1;
    }
    if (write(sock, request, strlen(request)) < 0) {
        perror("write request");
        close(sock);
        return 1;
    }

    if (mkdir_parent(output_path) != 0) {
        close(sock);
        return 1;
    }
    out = fopen(output_path, "wb");
    if (out == NULL) {
        perror(output_path);
        close(sock);
        return 1;
    }

    while ((count = read(sock, buffer, sizeof(buffer))) > 0) {
        char *body = buffer;
        size_t body_len = (size_t)count;
        if (!header_done) {
            size_t copy_len = body_len;
            char *marker;
            if (header_len + copy_len >= sizeof(header)) {
                copy_len = sizeof(header) - header_len - 1;
            }
            memcpy(header + header_len, buffer, copy_len);
            header_len += copy_len;
            header[header_len] = '\0';
            marker = strstr(header, "\r\n\r\n");
            if (marker == NULL) {
                continue;
            }
            header_done = 1;
            if (!starts_with(header, "HTTP/1.0 200") && !starts_with(header, "HTTP/1.1 200")) {
                fprintf(stderr, "HTTP GET failed for %s: %.64s\n", relative_path, header);
                status = 1;
                break;
            }
            body = marker + 4;
            body_len = header_len - (size_t)(body - header);
            if (body_len > 0 && fwrite(body, 1, body_len, out) != body_len) {
                perror(output_path);
                status = 1;
                break;
            }
            continue;
        }
        if (fwrite(body, 1, body_len, out) != body_len) {
            perror(output_path);
            status = 1;
            break;
        }
    }

    if (count < 0) {
        perror("read response");
        status = 1;
    }
    fclose(out);
    close(sock);
    if (status != 0) {
        unlink(output_path);
    }
    return status;
}

static int pull_list(const struct http_base *base, const char *workspace, const char *list_name) {
    char list_path[1024];
    char line[1024];
    FILE *list_file;
    int files = 0;

    if (mkdir_p(workspace) != 0) {
        return 1;
    }
    if (safe_join(list_path, sizeof(list_path), workspace, list_name) != 0) {
        return 1;
    }
    if (http_get_to_file(base, list_name, list_path) != 0) {
        return 1;
    }

    list_file = fopen(list_path, "rb");
    if (list_file == NULL) {
        perror(list_path);
        return 1;
    }
    while (fgets(line, sizeof(line), list_file) != NULL) {
        char output_path[1024];
        trim_line(line);
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
        if (safe_join(output_path, sizeof(output_path), workspace, line) != 0) {
            fclose(list_file);
            return 1;
        }
        printf("AUTODBG_HTTP_HELPER_GET %s\n", line);
        fflush(stdout);
        if (http_get_to_file(base, line, output_path) != 0) {
            fclose(list_file);
            return 1;
        }
        files++;
    }
    fclose(list_file);
    printf("AUTODBG_PULL_OK workspace=%s files=%d\n", workspace, files);
    return 0;
}

int main(int argc, char **argv) {
    struct http_base base;
    if (argc != 4) {
        fprintf(stderr, "Usage: %s http://host:port/base workspace list-name\n", argv[0]);
        return 2;
    }
    if (parse_base_url(argv[1], &base) != 0) {
        return 2;
    }
    return pull_list(&base, argv[2], argv[3]);
}
