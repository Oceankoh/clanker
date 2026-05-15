#!/usr/bin/env python3
"""
Solver for DiceCTF "blerg".

The clue is the exponential series:

    e = sum 1/n!

The 9 displayed numbers are a 3x3 matrix.  The nine buttons are the nine
elementary matrices E_ij, but exponentiated:

    exp(E_ij) = I + E_ij          (i != j)
    exp(E_ii) = diag(..., e, ...)

over GF(65537).  This script reads each round, computes the matrix gap from
values to target, decomposes that gap into the keypad generators, and streams
the corresponding digits.

It tries a few natural interpretations because the service can display either
the group element itself or its logarithm, and the generator can be applied on
the left or right.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time
from dataclasses import dataclass


HOST = "blerg.play.ctf.se"
PORT = 7331
P = 65537
ORDER_E = 32768
ANSI_RE = re.compile(rb"\x1b\[[0-9;]*m")
NUM_RE = re.compile(rb"\d+")


def inv(x: int) -> int:
    return pow(x % P, -1, P)


def add(a, b):
    return [[(a[i][j] + b[i][j]) % P for j in range(3)] for i in range(3)]


def smul(c, a):
    return [[(c * a[i][j]) % P for j in range(3)] for i in range(3)]


def mm(a, b):
    return [
        [sum(a[i][k] * b[k][j] for k in range(3)) % P for j in range(3)]
        for i in range(3)
    ]


def eye():
    return [[1 if i == j else 0 for j in range(3)] for i in range(3)]


def mat(xs):
    return [[xs[3 * i + j] % P for j in range(3)] for i in range(3)]


def flat(a):
    return [a[i][j] for i in range(3) for j in range(3)]


def transpose(a):
    return [[a[j][i] for j in range(3)] for i in range(3)]


def det(a):
    return (
        a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
    ) % P


def minv(a):
    n = 3
    m = [a[i][:] + [1 if i == j else 0 for j in range(n)] for i in range(n)]
    for c in range(n):
        pivot = next((r for r in range(c, n) if m[r][c] % P), None)
        if pivot is None:
            raise ValueError("singular matrix")
        m[c], m[pivot] = m[pivot], m[c]
        q = inv(m[c][c])
        m[c] = [(x * q) % P for x in m[c]]
        for r in range(n):
            if r == c:
                continue
            f = m[r][c]
            if f:
                m[r] = [(m[r][j] - f * m[c][j]) % P for j in range(2 * n)]
    return [row[n:] for row in m]


def charpoly_coeffs(a):
    # x^3 + c2*x^2 + c1*x + c0
    tr = (a[0][0] + a[1][1] + a[2][2]) % P
    c2 = (-tr) % P
    c1 = (
        a[0][0] * a[1][1]
        + a[0][0] * a[2][2]
        + a[1][1] * a[2][2]
        - a[0][1] * a[1][0]
        - a[0][2] * a[2][0]
        - a[1][2] * a[2][1]
    ) % P
    c0 = (-det(a)) % P
    return c0, c1, c2


def expm(a):
    """Truncated matrix exponential over GF(65537), reduced via Cayley-Hamilton."""
    c0, c1, c2 = charpoly_coeffs(a)
    poly = [1, 0, 0]  # x^n mod charpoly
    acc = [1, 0, 0]
    inv_fact = 1

    for n in range(1, P):
        inv_fact = (inv_fact * inv(n)) % P
        # multiply current degree<3 polynomial by x modulo x^3+c2*x^2+c1*x+c0
        poly = [
            (-poly[2] * c0) % P,
            (poly[0] - poly[2] * c1) % P,
            (poly[1] - poly[2] * c2) % P,
        ]
        acc[0] = (acc[0] + inv_fact * poly[0]) % P
        acc[1] = (acc[1] + inv_fact * poly[1]) % P
        acc[2] = (acc[2] + inv_fact * poly[2]) % P

    a2 = mm(a, a)
    return add(add(smul(acc[0], eye()), smul(acc[1], a)), smul(acc[2], a2))


def scalar_e():
    s = 0
    inv_fact = 1
    for n in range(P):
        if n:
            inv_fact = (inv_fact * inv(n)) % P
        s = (s + inv_fact) % P
    return s


E = scalar_e()
DLOG_E = {}
x = 1
for k in range(ORDER_E):
    DLOG_E[x] = k
    x = (x * E) % P


@dataclass(frozen=True)
class Op:
    digit: int
    count: int


def digit_of(r: int, c: int) -> int:
    return 3 * r + c + 1


def rc_of(digit: int) -> tuple[int, int]:
    z = digit - 1
    return z // 3, z % 3


def gen(op: Op):
    r, c = rc_of(op.digit)
    g = eye()
    if r == c:
        g[r][r] = pow(E, op.count % ORDER_E, P)
    else:
        g[r][c] = op.count % P
    return g


def normalize_ops(ops):
    out = []
    for op in ops:
        r, c = rc_of(op.digit)
        mod = ORDER_E if r == c else P
        count = op.count % mod
        if count:
            out.append(Op(op.digit, count))
    return out


def invert_op(op: Op) -> Op:
    r, c = rc_of(op.digit)
    mod = ORDER_E if r == c else P
    return Op(op.digit, (-op.count) % mod)


def transpose_op(op: Op) -> Op:
    r, c = rc_of(op.digit)
    return Op(digit_of(c, r), op.count)


def apply_left(m, op: Op):
    r, c = rc_of(op.digit)
    m = [row[:] for row in m]
    if r == c:
        scale = pow(E, op.count % ORDER_E, P)
        m[r] = [(scale * x) % P for x in m[r]]
    else:
        coeff = op.count % P
        m[r] = [(m[r][j] + coeff * m[c][j]) % P for j in range(3)]
    return m


def add_row_op(dst, src, coeff):
    return Op(digit_of(dst, src), coeff % P)


def scale_pair_ops(i, j, a):
    """Rows i,j: multiply row i by a and row j by a^-1 using transvections."""
    a %= P
    if a == 0:
        raise ValueError("cannot scale by zero")
    t = inv(a)
    return [
        add_row_op(i, j, t),
        add_row_op(j, i, -a),
        add_row_op(i, j, t),
        add_row_op(i, j, -1),
        add_row_op(j, i, 1),
        add_row_op(i, j, -1),
    ]


def row_reduce_ops_to_identity(d):
    """
    Return row-generator ops R such that R[-1]...R[0] * d = I.
    """
    m = [row[:] for row in d]
    ops = []

    def do(op):
        nonlocal m
        op = normalize_ops([op])
        if not op:
            return
        op = op[0]
        ops.append(op)
        m = apply_left(m, op)

    for i in range(3):
        if m[i][i] == 0:
            pivot = next((r for r in range(i + 1, 3) if m[r][i] != 0), None)
            if pivot is None:
                raise ValueError("no pivot")
            # signed row swap: (ri, rp) -> (rp, -ri)
            do(add_row_op(i, pivot, 1))
            do(add_row_op(pivot, i, -1))
            do(add_row_op(i, pivot, 1))

        for r in range(3):
            if r == i:
                continue
            if m[r][i] != 0:
                do(add_row_op(r, i, -m[r][i] * inv(m[i][i])))

    d1, d2, d3 = m[0][0], m[1][1], m[2][2]
    if not (d1 and d2 and d3):
        raise ValueError("bad diagonal after reduction")

    for op in scale_pair_ops(0, 1, inv(d1)):
        do(op)
    for op in scale_pair_ops(1, 2, inv((d1 * d2) % P)):
        do(op)

    remaining = m[2][2] % P
    need = inv(remaining)
    if need not in DLOG_E:
        raise ValueError(f"determinant scalar {need} is not generated by e")
    do(Op(digit_of(2, 2), DLOG_E[need]))

    if m != eye():
        raise AssertionError(f"reduction bug, got {m}")
    return ops


def word_for_left_matrix(d):
    reduction = row_reduce_ops_to_identity(d)
    word = [invert_op(op) for op in reversed(reduction)]
    product = eye()
    for op in word:
        product = mm(gen(op), product)
    if product != d:
        raise AssertionError("left word verification failed")
    return normalize_ops(word)


def word_for_right_matrix(d):
    # Row decomposition of transpose, then transpose each generator back.
    word_t = word_for_left_matrix(transpose(d))
    word = [transpose_op(op) for op in word_t]
    product = eye()
    for op in word:
        product = mm(product, gen(op))
    if product != d:
        raise AssertionError("right word verification failed")
    return normalize_ops(word)


def ops_to_digits(ops, max_len=None):
    n = sum(op.count for op in ops)
    if max_len is not None and n > max_len:
        raise ValueError(f"word too long: {n} digits")
    return "".join(str(op.digit) * op.count for op in ops)


def parse_latest(clean: bytes):
    vals = target = None
    round_no = None
    for line in clean.splitlines():
        if b"round " in line:
            m = re.search(rb"round\s+(\d+)/64", line)
            if m:
                round_no = int(m.group(1))
        elif b"values:" in line:
            vals = [int(x) for x in NUM_RE.findall(line.split(b":", 1)[1])]
        elif b"target:" in line:
            target = [int(x) for x in NUM_RE.findall(line.split(b":", 1)[1])]
    return round_no, vals, target


def recv_some(sock, timeout=0.25):
    old = sock.gettimeout()
    sock.settimeout(timeout)
    buf = b""
    while True:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except TimeoutError:
            break
        except socket.timeout:
            break
    sock.settimeout(old)
    return buf


def recv_until_round(sock):
    buf = b""
    while True:
        buf += sock.recv(4096)
        clean = ANSI_RE.sub(b"", buf)
        _, vals, target = parse_latest(clean)
        if vals is not None and target is not None:
            return buf


def candidate_ops(vals, target, mode):
    v = mat(vals)
    t = mat(target)

    if mode.startswith("exp-"):
        v = expm(v)
        t = expm(t)

    if mode.endswith("right"):
        gap = mm(minv(v), t)
        return word_for_right_matrix(gap)
    if mode.endswith("left"):
        gap = mm(t, minv(v))
        return word_for_left_matrix(gap)
    raise ValueError(mode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", default=PORT, type=int)
    ap.add_argument(
        "--modes",
        default="raw-right,raw-left,exp-right,exp-left",
        help="comma-separated model order",
    )
    ap.add_argument("--max-word", type=int, default=900000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    with socket.create_connection((args.host, args.port), timeout=10) as sock:
        sock.settimeout(10)
        buf = recv_until_round(sock)

        while True:
            clean = ANSI_RE.sub(b"", buf)
            print(clean.decode(errors="replace"), end="")
            round_no, vals, target = parse_latest(clean)
            if vals is None or target is None:
                print("[!] no round found", file=sys.stderr)
                return

            solved_attempt = False
            for mode in modes:
                try:
                    ops = candidate_ops(vals, target, mode)
                    digits = ops_to_digits(ops, args.max_word)
                except Exception as exc:
                    print(f"[.] {mode} skipped: {exc}", file=sys.stderr)
                    continue

                print(
                    f"[+] round {round_no}: trying {mode}, "
                    f"{len(ops)} compressed ops, {len(digits)} keypresses",
                    file=sys.stderr,
                )
                if args.dry_run:
                    print(digits[:200] + ("..." if len(digits) > 200 else ""))
                    return

                # One key per line; the server accepts buffered input.
                payload = ("\n".join(digits) + "\n").encode()
                sock.sendall(payload)
                time.sleep(0.2)
                buf = recv_some(sock, timeout=0.6)
                if not buf:
                    print("[!] connection closed", file=sys.stderr)
                    return

                new_clean = ANSI_RE.sub(b"", buf)
                new_round, new_vals, new_target = parse_latest(new_clean)
                if b"failed" in new_clean:
                    print(new_clean.decode(errors="replace"), end="")
                    print("[!] failed", file=sys.stderr)
                    return

                # If the round changed, this mode worked.  If not, keep the new
                # current state and try the next interpretation.
                if new_round is not None and new_round != round_no:
                    solved_attempt = True
                    break
                if new_target is not None and new_target != target:
                    solved_attempt = True
                    break
                if new_vals is not None:
                    vals = new_vals
                    buf = new_clean
                    print(f"[-] {mode} did not advance; trying next model", file=sys.stderr)

            if not solved_attempt:
                print("[!] no model advanced the round", file=sys.stderr)
                return


if __name__ == "__main__":
    main()
