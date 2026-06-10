/* ret2win stack overflow for the clanker gdb-MCP lab challenge.
 * x86-64, compiled -no-pie -fno-stack-protector so win() has a fixed address
 * and the saved return pointer is reachable. The flag is XOR-0x37 obfuscated so
 * `strings` won't reveal it — you must redirect execution into win() (or read
 * it out dynamically with the debugger). */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

void win(void) {
    unsigned char enc[] = { 0x51, 0x5b, 0x56, 0x50, 0x4c, 0x45, 0x04, 0x43, 0x05, 0x40, 0x06, 0x59, 0x68, 0x50, 0x53, 0x55, 0x68, 0x5a, 0x54, 0x47, 0x4a };
    for (unsigned i = 0; i < sizeof(enc); i++) putchar(enc[i] ^ 0x37);
    putchar('\n');
    fflush(stdout);
    _exit(0);
}

void vuln(void) {
    char buf[64];
    puts("data> ");
    /* reads far more than buf holds -> overflow the saved return address */
    read(0, buf, 256);
    printf("you said: %s\n", buf);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    puts("ret2win: overflow buf and redirect execution to win()");
    vuln();
    return 0;
}
