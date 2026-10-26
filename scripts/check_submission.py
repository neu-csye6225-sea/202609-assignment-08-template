#!/usr/bin/env python3
"""Assignment 8 checker: DNS for Your Library API.

Used two ways:
  * by the Gradescope autograder (grade.py imports run_checks), and
  * by students, from the repo root:   python3 scripts/check_submission.py

Standard library only (Python 3.10+): no pip install needed.
"""
from __future__ import annotations

import json
import os
import random
import re
import socket
import struct
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------
# Settings (environment overrides exist only for the instructor's offline tests)
# ----------------------------------------------------------------------------
RESOLVERS = os.environ.get("A8_RESOLVERS", "8.8.8.8,1.1.1.1").split(",")
DNS_PORT = int(os.environ.get("A8_DNS_PORT", "53"))
API_PORT = int(os.environ.get("A8_API_PORT", "8080"))
TIMEOUT = float(os.environ.get("A8_TIMEOUT", "4"))
GRADER_USER = "grader"
GRADER_PASSWORD = "Library-Fall2026"
REQUIRED_TTL = 60
ROUTE53_NS = re.compile(r"^ns-\d+\.awsdns-\d+\.(com|net|org|co\.uk)$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PLACEHOLDERS = {"yourdomain.me", "example.me", "<yourdomain>.me", "domain.me"}
# Students must use <name>.me (so the API is api.<name>.me). The instructors' own domains are also accepted.
STUDENT_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.me$")
INSTRUCTOR_DOMAINS = {"madchili.com", "sushisoft.com"}

T_A, T_NS, T_CNAME, T_SOA = 1, 2, 5, 6
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}


# ----------------------------------------------------------------------------
# Minimal DNS client (UDP with TCP fallback)
# ----------------------------------------------------------------------------
@dataclass
class RR:
    name: str
    rtype: int
    ttl: int
    data: object


@dataclass
class Reply:
    rcode: int
    aa: bool
    answer: list = field(default_factory=list)
    authority: list = field(default_factory=list)

    @property
    def rcode_name(self) -> str:
        return RCODES.get(self.rcode, str(self.rcode))


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        if label:
            out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\x00"


def _read_name(msg: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end, hops = [], False, off, 0
    while True:
        if off >= len(msg) or hops > 64:
            raise ValueError("bad DNS name")
        n = msg[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = ((n & 0x3F) << 8) | msg[off + 1]
            if not jumped:
                end = off + 2
            jumped, off, hops = True, ptr, hops + 1
            continue
        labels.append(msg[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
    return ".".join(labels).lower(), (end if jumped else off)


def _parse(msg: bytes) -> Reply:
    _id, flags, qd, an, ns, _ar = struct.unpack("!HHHHHH", msg[:12])
    off = 12
    for _ in range(qd):
        _, off = _read_name(msg, off)
        off += 4
    rep = Reply(rcode=flags & 0xF, aa=bool(flags & 0x0400))
    for count, bucket in ((an, rep.answer), (ns, rep.authority)):
        for _ in range(count):
            name, off = _read_name(msg, off)
            rtype, _cls, ttl, rdlen = struct.unpack("!HHIH", msg[off:off + 10])
            off += 10
            rd = msg[off:off + rdlen]
            if rtype == T_A and rdlen == 4:
                data = socket.inet_ntoa(rd)
            elif rtype in (T_NS, T_CNAME):
                data, _ = _read_name(msg, off)
            elif rtype == T_SOA:
                mname, o = _read_name(msg, off)
                data = mname
            else:
                data = rd
            bucket.append(RR(name, rtype, ttl, data))
            off += rdlen
    return rep


def dns_query(name: str, qtype: int, server: str, recursive: bool) -> Reply:
    """One query to one server. Raises OSError on timeout/network failure."""
    qid = random.randint(0, 0xFFFF)
    flags = 0x0100 if recursive else 0
    pkt = struct.pack("!HHHHHH", qid, flags, 1, 0, 0, 0) + _encode_name(name) + struct.pack("!HH", qtype, 1)
    data = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(TIMEOUT)
            s.sendto(pkt, (server, DNS_PORT))
            data, _ = s.recvfrom(4096)
            if struct.unpack("!H", data[:2])[0] != qid:
                data = None
    except OSError:
        data = None
    if data is None or (struct.unpack("!H", data[2:4])[0] & 0x0200):  # no reply or truncated: use TCP
        with socket.create_connection((server, DNS_PORT), timeout=TIMEOUT) as s:
            s.sendall(struct.pack("!H", len(pkt)) + pkt)
            ln = struct.unpack("!H", _recv_exact(s, 2))[0]
            data = _recv_exact(s, ln)
    return _parse(data)


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed")
        buf += chunk
    return buf


def recursive_query(name: str, qtype: int) -> Reply:
    last = None
    for r in RESOLVERS:
        try:
            return dns_query(name, qtype, r.strip(), recursive=True)
        except OSError as e:
            last = e
    raise OSError(f"no public resolver answered ({last})")


def host_ips(host: str) -> list[str]:
    try:
        rep = recursive_query(host, T_A)
        ips = [rr.data for rr in rep.answer if rr.rtype == T_A]
        if ips:
            return ips
    except OSError:
        pass
    try:
        return socket.gethostbyname_ex(host)[2]
    except OSError:
        return []


# ----------------------------------------------------------------------------
# Submission files
# ----------------------------------------------------------------------------
def normalize_domain(raw: str) -> str:
    d = raw.strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split(":")[0].rstrip(".")
    if d.startswith("api."):
        d = d[4:]
    if d.startswith("www."):
        d = d[4:]
    return d


def read_domain(root: Path) -> tuple[str | None, str]:
    p = root / "domain.txt"
    if not p.exists():
        return None, "domain.txt is missing from the repository root."
    lines = [l.strip() for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return None, "domain.txt is empty. Put your registered domain on one line, e.g. jdoe-csye.me"
    d = normalize_domain(lines[0])
    if d in PLACEHOLDERS:
        return None, f"domain.txt still contains the placeholder '{lines[0]}'. Replace it with your domain."
    if not DOMAIN_RE.match(d):
        return None, f"'{lines[0]}' is not a valid domain name."
    if d not in INSTRUCTOR_DOMAINS and not STUDENT_DOMAIN_RE.match(d):
        return None, (f"'{d}' is not allowed. Use a domain of the form <name>.me (for example jdoe-csye.me), "
                      f"so your API is api.<name>.me: exactly three parts, 'api', your name, and 'me'.")
    return d, f"Domain: {d} (API name: api.{d})"


def read_instance_ip(root: Path) -> tuple[str | None, str]:
    p = root / "terraform" / "terraform.tfstate"
    if not p.exists():
        return None, "terraform/terraform.tfstate is missing. Copy terraform/ from your Assignment 7 repo (state included) and commit it."
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return None, f"terraform/terraform.tfstate is not valid JSON ({e})."
    out = (st.get("outputs") or {}).get("instance_public_ip", {}).get("value")
    found = []
    for res in st.get("resources", []):
        if res.get("mode") == "managed" and res.get("type") == "aws_instance":
            for inst in res.get("instances", []):
                a = inst.get("attributes", {}) or {}
                if a.get("public_ip"):
                    name = (a.get("tags") or {}).get("Name", "")
                    found.append((name == "a7-library-api", a["public_ip"]))
    if found:
        found.sort(reverse=True)
        return found[0][1], f"Instance public IP in terraform.tfstate: {found[0][1]}"
    if out:
        return out, f"Instance public IP in terraform.tfstate: {out}"
    return None, "terraform.tfstate has no EC2 instance with a public IP. Run `terraform apply` and commit the state file."


# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    max_score: int
    score: int = 0
    output: str = ""

    def ok(self, msg: str) -> None:
        self.score, self.output = self.max_score, "PASS: " + msg

    def fail(self, msg: str) -> None:
        self.score, self.output = 0, "FAIL: " + msg


def _http(method: str, url: str, host_header: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Host", host_header)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT + 4) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def run_checks(root: Path) -> list[Check]:
    c_dom = Check("1. domain.txt names your domain", 1)
    c_tf = Check("2. terraform.tfstate has your instance's public IP", 1)
    c_del = Check("3. Domain is delegated to Amazon Route 53", 4)
    c_zone = Check("4. Route 53 hosted zone answers for the domain", 2)
    c_rec = Check("5. api.<domain> A record exists", 3)
    c_ttl = Check("6. api.<domain> TTL is 60 seconds", 1)
    c_ip = Check("7. api.<domain> points to your instance", 3)
    c_hc = Check("8. GET http://api.<domain>:8080/healthcheck", 3)
    c_api = Check("9. Log in and GET /v1/books through api.<domain>", 2)
    checks = [c_dom, c_tf, c_del, c_zone, c_rec, c_ttl, c_ip, c_hc, c_api]

    domain, msg = read_domain(root)
    (c_dom.ok if domain else c_dom.fail)(msg)
    inst_ip, msg = read_instance_ip(root)
    (c_tf.ok if inst_ip else c_tf.fail)(msg)
    if not domain:
        for c in checks[2:]:
            c.fail("Skipped: fix domain.txt first.")
        return checks
    api = f"api.{domain}"

    # --- 3. delegation at the parent (TLD) servers ---
    parent = domain.split(".", 1)[1]
    ns_names, tld_note = [], ""
    try:
        tld_servers = [rr.data for rr in recursive_query(parent, T_NS).answer if rr.rtype == T_NS]
        for tld_ns in tld_servers[:4]:
            for ip in host_ips(tld_ns)[:1]:
                try:
                    rep = dns_query(domain, T_NS, ip, recursive=False)
                except OSError:
                    continue
                if rep.rcode == 3:
                    tld_note = f"The .{parent} registry says {domain} does not exist (NXDOMAIN). Is it registered and spelled correctly?"
                ns_names = sorted({rr.data for rr in rep.answer + rep.authority
                                   if rr.rtype == T_NS and rr.name == domain})
                break
            if ns_names or tld_note:
                break
        if not tld_servers:
            tld_note = f"Could not find the name servers for .{parent}."
    except OSError as e:
        tld_note = f"DNS lookup failed: {e}"

    delegated = bool(ns_names) and all(ROUTE53_NS.match(n) for n in ns_names)
    if delegated:
        c_del.ok(f"The .{parent} registry delegates {domain} to: {', '.join(ns_names)}")
    elif ns_names:
        c_del.fail(f"{domain} is delegated to {', '.join(ns_names)}, not to Route 53. "
                   "At your registrar, choose Custom DNS and enter the 4 NS values from your Route 53 hosted zone. "
                   "The change can take up to a few hours to appear.")
    else:
        c_del.fail(tld_note or f"No NS delegation found for {domain}.")

    # --- 4. the hosted zone itself answers authoritatively ---
    auth_ips: list[str] = []
    if delegated:
        problems = []
        for n in ns_names:
            for ip in host_ips(n)[:1]:
                try:
                    rep = dns_query(domain, T_SOA, ip, recursive=False)
                except OSError as e:
                    problems.append(f"{n}: no reply ({e})")
                    continue
                if rep.aa and rep.rcode == 0 and any(rr.rtype == T_SOA for rr in rep.answer):
                    auth_ips.append(ip)
                else:
                    problems.append(f"{n}: {rep.rcode_name}")
        if auth_ips:
            c_zone.ok(f"{len(auth_ips)} of {len(ns_names)} Route 53 name servers answer authoritatively for {domain}.")
        else:
            c_zone.fail("The Route 53 name servers set at your registrar do not host a zone for "
                        f"{domain} ({'; '.join(problems)}). If you deleted and re-created the hosted zone, "
                        "it has new name servers: copy the new NS values to your registrar.")
    else:
        c_zone.fail("Skipped: the domain is not delegated to Route 53 yet (check 3).")

    # --- 5/6. the api A record ---
    a_ips, ttl, source = [], None, ""
    try:
        if auth_ips:
            rep = dns_query(api, T_A, auth_ips[0], recursive=False)
            source = "Route 53 (authoritative)"
        else:
            rep = recursive_query(api, T_A)
            source = "a public resolver"
        a_rrs = [rr for rr in rep.answer if rr.rtype == T_A and rr.name == api]
        cnames = [rr for rr in rep.answer if rr.rtype == T_CNAME and rr.name == api]
        a_ips = sorted({rr.data for rr in a_rrs})
        ttl = a_rrs[0].ttl if (a_rrs and auth_ips) else None
        if a_ips:
            c_rec.ok(f"{api} → {', '.join(a_ips)} (from {source})")
        elif cnames:
            c_rec.fail(f"{api} is a CNAME to {cnames[0].data}. Create an A record whose value is your instance's public IP.")
        elif rep.rcode == 3:
            c_rec.fail(f"{api} does not exist (NXDOMAIN from {source}). Create an A record named 'api' in your hosted zone.")
        else:
            c_rec.fail(f"No A record for {api} ({rep.rcode_name} from {source}).")
    except OSError as e:
        c_rec.fail(f"DNS lookup for {api} failed: {e}")

    if ttl is None:
        c_ttl.fail("Skipped: needs the A record from your Route 53 name servers (checks 4 and 5).")
    elif ttl == REQUIRED_TTL:
        c_ttl.ok(f"TTL is {ttl} seconds.")
    else:
        c_ttl.fail(f"TTL is {ttl} seconds; set it to {REQUIRED_TTL}.")

    # --- 7. the record points at the instance in terraform.tfstate ---
    if not a_ips:
        c_ip.fail("Skipped: no A record (check 5).")
    elif not inst_ip:
        c_ip.fail("Skipped: no instance IP in terraform.tfstate (check 2).")
    elif a_ips == [inst_ip]:
        c_ip.ok(f"{api} points to {inst_ip}, the instance in your terraform.tfstate.")
    elif inst_ip in a_ips:
        c_ip.fail(f"{api} has several values ({', '.join(a_ips)}). Leave only {inst_ip}.")
    else:
        c_ip.fail(f"{api} points to {', '.join(a_ips)}, but your terraform.tfstate instance is {inst_ip}. "
                  "If Terraform replaced your instance, update the A record with the new IP "
                  "(`terraform output -raw instance_public_ip`) and commit the new state file.")

    # --- 8/9. the API, reached by name ---
    if not a_ips:
        c_hc.fail("Skipped: no A record (check 5).")
        c_api.fail("Skipped: no A record (check 5).")
        return checks
    target = a_ips[0]
    base, host_hdr = f"http://{target}:{API_PORT}", f"{api}:{API_PORT}"
    try:
        status, body = _http("GET", f"{base}/healthcheck", host_hdr)
        try:
            parsed = json.loads(body or b"null")
        except ValueError:
            parsed = None
        if status == 200 and parsed == {"status": "ok"}:
            c_hc.ok(f'http://{api}:{API_PORT}/healthcheck returned 200 {{"status": "ok"}}')
        else:
            c_hc.fail(f'http://{api}:{API_PORT}/healthcheck returned {status} {body[:120]!r}; expected 200 {{"status": "ok"}}')
    except OSError as e:
        c_hc.fail(f"Could not connect to http://{api}:{API_PORT} ({target}): {e}. "
                  "Is the instance running, and does its security group allow port 8080?")
        c_api.fail("Skipped: the server is not reachable (check 8).")
        return checks

    try:
        status, body = _http("POST", f"{base}/v1/login", host_hdr, {"username": GRADER_USER, "password": GRADER_PASSWORD})
        token = (json.loads(body or b"{}") or {}).get("access_token") if status == 200 else None
        if not token:
            c_api.fail(f"POST /v1/login as '{GRADER_USER}' returned {status}; expected 200 with an access_token.")
        else:
            status, body = _http("GET", f"{base}/v1/books", host_hdr, token=token)
            json.loads(body or b"null")
            if status == 200:
                c_api.ok("Logged in as the grader and GET /v1/books returned 200.")
            else:
                c_api.fail(f"GET /v1/books returned {status}; expected 200.")
    except (OSError, ValueError, AttributeError) as e:
        c_api.fail(f"API request failed: {e}")
    return checks


# ----------------------------------------------------------------------------
# Student command line
# ----------------------------------------------------------------------------
def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    print(f"Checking the Assignment 8 submission in {root}\n")
    checks = run_checks(root)
    total = sum(c.score for c in checks)
    for c in checks:
        print(f"[{c.score}/{c.max_score}] {c.name}\n        {c.output}\n")
    print(f"Score: {total} / {sum(c.max_score for c in checks)}")
    print("This is the same check the Gradescope autograder runs.")
    return 0 if total == sum(c.max_score for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
