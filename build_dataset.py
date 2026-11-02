import argparse, csv, json, math, os, random, email
from email import policy

# Faker is required: without it the script would generate a different dataset, and results
# from different machines would no longer be comparable. Install it with: pip install faker
from faker import Faker
FAKER = True

DEPARTMENTS = ["Engineering", "Finance", "Legal", "HR", "Sales", "Marketing",
               "Operations", "Support", "Research", "Compliance"]

# assumed revocations per user per working day (see scenario table in the report)
SCENARIOS = {"floor": 0.0015, "low": 0.01, "medium": 0.1, "high": 0.5}
WORK_MINUTES = 480


# ---------------------------------------------------------------- users / groups
def make_org(n_users, rng):
    fake = Faker() if FAKER else None
    if FAKER:
        Faker.seed(rng.randint(0, 10**9))
    first = ["Asha", "Ravi", "Meera", "Arjun", "Sana", "Kiran", "Neha", "Rohan", "Isha", "Vikram"]
    last = ["Patil", "Shah", "Rao", "Khan", "Iyer", "Joshi", "Nair", "Singh", "Das", "Kulkarni"]
    users, groups, members = [], [], []

    # one group per department + project groups
    for i, d in enumerate(DEPARTMENTS):
        groups.append({"group_id": f"g_dept_{i}", "name": d, "type": "department"})
    n_projects = max(5, n_users // 20)
    for p in range(n_projects):
        groups.append({"group_id": f"g_proj_{p}", "name": f"Project-{p}", "type": "project"})

    for u in range(n_users):
        dept = rng.randrange(len(DEPARTMENTS))
        name = fake.name() if FAKER else f"{rng.choice(first)} {rng.choice(last)}"
        users.append({"user_id": f"u{u}", "name": name, "department": DEPARTMENTS[dept],
                      "role": rng.choice(["analyst", "manager", "engineer", "associate", "lead"]),
                      "status": "active"})
        members.append({"user_id": f"u{u}", "group_id": f"g_dept_{dept}"})
        for p in rng.sample(range(n_projects), k=rng.randint(0, 3)):  # 0-3 project groups each
            members.append({"user_id": f"u{u}", "group_id": f"g_proj_{p}"})
    return users, groups, members


# ---------------------------------------------------------------- documents
def load_texts(args, rng):
    n = args.docs
    if args.source == "synthetic":
        fake = Faker() if FAKER else None
        words = ("policy contract budget quarterly review security access audit project report "
                 "client vendor schedule invoice compliance risk forecast meeting minutes").split()
        for i in range(n):
            if FAKER:
                yield f"Doc {i}", " ".join(fake.paragraph(nb_sentences=40) for _ in range(rng.randint(1, 3)))
            else:
                yield f"Doc {i}", " ".join(rng.choice(words) for _ in range(rng.randint(300, 1200)))
    elif args.source == "20ng":
        from sklearn.datasets import fetch_20newsgroups
        data = fetch_20newsgroups(subset="all", remove=("headers", "footers", "quotes"))
        idx = [i for i, t in enumerate(data.data) if len(t.split()) > 80]
        rng.shuffle(idx)
        for i in idx[:n]:
            yield f"20ng-{i}", data.data[i]
    elif args.source == "wikipedia":
        from datasets import load_dataset
        ds = load_dataset("wikimedia/wikipedia", "20231101.en", split="train", streaming=True)
        for k, row in enumerate(ds):
            if k >= n:
                break
            yield row["title"], row["text"]
    elif args.source == "enron":
        count = 0
        for root, _, files in os.walk(args.enron_path):
            if not root.endswith("_sent_mail"):
                continue
            for f in files:
                try:
                    with open(os.path.join(root, f), "rb") as fh:
                        msg = email.message_from_binary_file(fh, policy=policy.default)
                    body = msg.get_body(preferencelist=("plain",))
                    text = body.get_content() if body else ""
                except Exception:
                    continue
                if len(text.split()) > 40:
                    yield msg["subject"] or f, text
                    count += 1
                    if count >= n:
                        return
    elif args.source == "dir":
        count = 0
        for root, _, files in os.walk(args.text_dir):
            for f in files:
                if f.endswith(".txt"):
                    with open(os.path.join(root, f), errors="ignore") as fh:
                        yield f, fh.read()
                    count += 1
                    if count >= n:
                        return
    else:
        raise SystemExit(f"unknown source {args.source}")


def chunk(text, size, overlap):
    w = text.split()
    step = max(1, size - overlap)
    for s in range(0, max(1, len(w)), step):
        piece = w[s:s + size]
        if len(piece) >= 20 or s == 0:
            yield " ".join(piece)
        if s + size >= len(w):
            break


# ---------------------------------------------------------------- ACLs
def assign_acl(rng, users, groups):
    """60% public, 30% group-level, 10% individual."""
    r = rng.random()
    if r < 0.60:
        return [("public", "*")]
    if r < 0.90:
        k = rng.randint(1, 3)
        return [("group", g["group_id"]) for g in rng.sample(groups, k)]
    k = rng.randint(1, 3)
    return [("user", u["user_id"]) for u in rng.sample(users, min(k, len(users)))]


# ---------------------------------------------------------------- revocation events
def make_events(rng, users, members, acl_rows, rate_per_min, duration_min, burst):
    """Poisson arrivals. Each event revokes access to a RESTRICTED doc; 3 types."""
    by_group = {}
    for m in members:
        by_group.setdefault(m["group_id"], []).append(m["user_id"])
    restricted = [a for a in acl_rows if a["principal_type"] != "public"]
    events, t = [], 0.0
    if rate_per_min > 0 and restricted:
        while True:
            t += rng.expovariate(rate_per_min)
            if t > duration_min:
                break
            a = rng.choice(restricted)
            if a["principal_type"] == "user":
                kind, uid = "user_deny", a["principal_id"]
            else:
                gid = a["principal_id"]
                uid = rng.choice(by_group.get(gid, ["u0"]))
                kind = rng.choice(["group_removal", "doc_acl_change"])
            events.append({"t_minute": round(t, 4), "event_type": kind,
                           "user_id": uid, "doc_id": a["doc_id"], "principal_id": a["principal_id"]})
    if burst > 0 and restricted:  # mass offboarding: `burst` revocations inside one minute
        t0 = rng.uniform(0, max(1, duration_min - 1))
        for _ in range(burst):
            a = rng.choice(restricted)
            events.append({"t_minute": round(t0 + rng.random(), 4), "event_type": "user_deny",
                           "user_id": rng.choice(users)["user_id"], "doc_id": a["doc_id"],
                           "principal_id": a["principal_id"]})
    events.sort(key=lambda e: e["t_minute"])
    for i, e in enumerate(events):
        e["event_id"] = i
    return events


# ---------------------------------------------------------------- io helpers
def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


SCHEMA = """-- Run: psql -d ragstudy -f schema.sql   (from inside the output folder)
CREATE TABLE users(user_id text PRIMARY KEY, name text, department text, role text, status text);
CREATE TABLE groups(group_id text PRIMARY KEY, name text, type text);
CREATE TABLE group_members(user_id text, group_id text, updated_at timestamptz DEFAULT now(),
                           PRIMARY KEY(user_id, group_id));
CREATE TABLE documents(doc_id text PRIMARY KEY, title text, classification text, n_chunks int);
CREATE TABLE doc_acl(doc_id text, principal_type text, principal_id text,
                     permission text DEFAULT 'read', acl_version int DEFAULT 1,
                     updated_at timestamptz DEFAULT now());
CREATE TABLE perm_audit(event_id serial PRIMARY KEY, user_id text, doc_id text,
                        action text, t_revocation timestamptz);
\\copy users FROM 'users.csv' CSV HEADER
\\copy groups FROM 'groups.csv' CSV HEADER
\\copy group_members(user_id, group_id) FROM 'group_members.csv' CSV HEADER
\\copy documents FROM 'documents.csv' CSV HEADER
\\copy doc_acl(doc_id, principal_type, principal_id) FROM 'doc_acl.csv' CSV HEADER
"""


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=1000)
    ap.add_argument("--docs", type=int, default=2000)
    ap.add_argument("--source", default="synthetic",
                    choices=["synthetic", "20ng", "wikipedia", "enron", "dir"])
    ap.add_argument("--enron-path", default="maildir")
    ap.add_argument("--text-dir", default="texts")
    ap.add_argument("--chunk-words", type=int, default=300, help="~400 tokens")
    ap.add_argument("--overlap", type=int, default=30)
    ap.add_argument("--scenario", choices=list(SCENARIOS), default="medium")
    ap.add_argument("--rate", type=float, default=None,
                    help="override: revocations per MINUTE for the whole population")
    ap.add_argument("--duration-min", type=int, default=60, help="length of event stream")
    ap.add_argument("--burst", type=int, default=0, help="extra mass-revocation size in one minute")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)

    users, groups, members = make_org(args.users, rng)

    docs, acl_rows, n_chunks = [], [], 0
    with open(os.path.join(args.out, "chunks.jsonl"), "w") as cf:
        for i, (title, text) in enumerate(load_texts(args, rng)):
            doc_id = f"d{i}"
            acl = assign_acl(rng, users, groups)
            for ptype, pid in acl:
                acl_rows.append({"doc_id": doc_id, "principal_type": ptype, "principal_id": pid})
            cls = "public" if acl[0][0] == "public" else "restricted"
            pieces = list(chunk(text, args.chunk_words, args.overlap))
            for j, c in enumerate(pieces):
                cf.write(json.dumps({
                    "chunk_id": f"{doc_id}_c{j}", "doc_id": doc_id, "text": c,
                    "allowed_principals": [f"{t}:{p}" for t, p in acl], "acl_version": 1}) + "\n")
            n_chunks += len(pieces)
            docs.append({"doc_id": doc_id, "title": str(title)[:200], "classification": cls,
                         "n_chunks": len(pieces)})

    rate = args.rate if args.rate is not None else args.users * SCENARIOS[args.scenario] / WORK_MINUTES
    events = make_events(rng, users, members, acl_rows, rate, args.duration_min, args.burst)

    write_csv(f"{args.out}/users.csv", users, ["user_id", "name", "department", "role", "status"])
    write_csv(f"{args.out}/groups.csv", groups, ["group_id", "name", "type"])
    write_csv(f"{args.out}/group_members.csv", members, ["user_id", "group_id"])
    write_csv(f"{args.out}/documents.csv", docs, ["doc_id", "title", "classification", "n_chunks"])
    write_csv(f"{args.out}/doc_acl.csv", acl_rows, ["doc_id", "principal_type", "principal_id"])
    write_csv(f"{args.out}/revocation_events.csv", events,
              ["event_id", "t_minute", "event_type", "user_id", "doc_id", "principal_id"])
    with open(f"{args.out}/schema.sql", "w") as f:
        f.write(SCHEMA)

    restricted = sum(d["classification"] == "restricted" for d in docs)
    print(f"users={len(users)} groups={len(groups)} memberships={len(members)}")
    print(f"documents={len(docs)} (restricted={restricted}) chunks={n_chunks}")
    print(f"revocation rate={rate:.4f}/min over {args.duration_min} min -> {len(events)} events"
          f"{' (incl. burst of %d)' % args.burst if args.burst else ''}")
    print(f"written to ./{args.out}/")


if __name__ == "__main__":
    main()
