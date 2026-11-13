/* 公开测试夹具：构建时注入随机编码，测试必须从工具观察恢复输入。 */
#include <stdio.h>
#include <string.h>
#include <signal.h>
#include "encoded.h"
__attribute__((noinline)) int check(const char *input) {
    if (strlen(input) != sizeof(encoded)) return 0;
    for (unsigned i = 0; i < sizeof(encoded); ++i)
        if (((unsigned char)input[i] ^ 0x5a) != encoded[i]) return 0;
    return 1;
}
int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "--crash")) { raise(SIGSEGV); return 2; }
    if (argc > 1 && !strcmp(argv[1], "--loop")) { for (;;) {} }
    char line[128];
    const char *input = argc > 1 ? argv[1] : line;
    if (argc < 2) {
        if (!fgets(line, sizeof(line), stdin)) return 3;
        line[strcspn(line, "\n")] = 0;
    }
    int valid = check(input);
    puts(valid ? "accepted" : "rejected");
    return valid ? 0 : 1;
}
