import argparse
import os
import re

import pandas as pd

report = []


def log(check, before, after, action):
    report.append({"check": check, "rows_before": before, "rows_after": after,
                   "rows_affected": before - after, "action": action})
    print(f"{check:<45} affected: {before - after}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data_raw")
    ap.add_argument("--out", default="data_clean")
    ap.add_argument("--min-words", type=int, default=20, help="drop chunks shorter than this")
    a = ap.parse_args()

    users = pd.read_csv(f"{a.inp}/users.csv")
    groups = pd.read_csv(f"{a.inp}/groups.csv")
    members = pd.read_csv(f"{a.inp}/group_members.csv")
    docs = pd.read_csv(f"{a.inp}/documents.csv")
    acl = pd.read_csv(f"{a.inp}/doc_acl.csv")
    chunks = pd.read_json(f"{a.inp}/chunks.jsonl", lines=True)

    # ---- users
    n = len(users); users = users.drop_duplicates("user_id", keep="first")
    log("duplicate user_id", n, len(users), "kept first")
    n = len(users); users = users.dropna(subset=["user_id", "department", "role"])
    log("users with missing department/role", n, len(users), "dropped")

    # ---- group membership must point to real users and groups
    n = len(members); members = members.drop_duplicates()
    log("duplicate memberships", n, len(members), "dropped")
    n = len(members)
    members = members[members["user_id"].isin(users["user_id"]) & members["group_id"].isin(groups["group_id"])]
    log("memberships with unknown user/group", n, len(members), "dropped")

    # ---- documents
    n = len(docs); docs = docs.drop_duplicates("doc_id", keep="first")
    log("duplicate doc_id", n, len(docs), "kept first")

    # ---- chunks: text cleaning
    old = chunks["text"].fillna("").astype(str)
    chunks["text"] = old.map(lambda t: re.sub(r"\s+", " ", t).strip())
    changed = int((old != chunks["text"]).sum())
    report.append({"check": "chunk text whitespace normalised", "rows_before": len(chunks),
                   "rows_after": len(chunks), "rows_affected": changed, "action": "extra spaces/newlines collapsed"})
    print(f"{'chunk text whitespace normalised':<45} affected: {changed}")
    n = len(chunks); chunks = chunks[chunks["text"].str.split().str.len() >= a.min_words]
    log(f"chunks shorter than {a.min_words} words / empty", n, len(chunks), "dropped")
    n = len(chunks); chunks = chunks.drop_duplicates("chunk_id", keep="first")
    log("duplicate chunk_id", n, len(chunks), "kept first")
    n = len(chunks); chunks = chunks.drop_duplicates("text", keep="first")
    log("duplicate chunk text", n, len(chunks), "kept first")
    n = len(chunks); chunks = chunks[chunks["doc_id"].isin(docs["doc_id"])]
    log("chunks whose document does not exist", n, len(chunks), "dropped")

    # ---- documents must still have at least one chunk
    counts = chunks.groupby("doc_id").size()
    n = len(docs); docs = docs[docs["doc_id"].isin(counts.index)].copy()
    docs["n_chunks"] = docs["doc_id"].map(counts)
    log("documents left with no chunks", n, len(docs), "dropped; n_chunks recalculated")

    # ---- ACL rows must point to real documents and real principals
    n = len(acl); acl = acl.drop_duplicates()
    log("duplicate ACL rows", n, len(acl), "dropped")
    n = len(acl); acl = acl[acl["doc_id"].isin(docs["doc_id"])]
    log("ACL rows for unknown document", n, len(acl), "dropped")
    n = len(acl)
    valid = (acl["principal_type"].eq("public")
             | (acl["principal_type"].eq("user") & acl["principal_id"].isin(users["user_id"]))
             | (acl["principal_type"].eq("group") & acl["principal_id"].isin(groups["group_id"])))
    acl = acl[valid]
    log("ACL rows for unknown user/group", n, len(acl), "dropped")

    # ---- every document needs an ACL, otherwise nobody can say who may read it
    n = len(docs); docs = docs[docs["doc_id"].isin(acl["doc_id"])]
    chunks = chunks[chunks["doc_id"].isin(docs["doc_id"])]
    log("documents with no ACL", n, len(docs), "dropped (with their chunks)")

    # ---- every chunk must carry the SAME access list as the ACL table (stale-ACL check)
    if "allowed_principals" in chunks.columns:
        want = (acl.assign(p=acl["principal_type"] + ":" + acl["principal_id"])
                   .groupby("doc_id")["p"].apply(lambda s: sorted(s)))
        new = chunks["doc_id"].map(want)
        old = chunks["allowed_principals"].apply(lambda l: tuple(sorted(l)) if isinstance(l, list) else ())
        changed = int((old != new.apply(lambda l: tuple(l) if isinstance(l, list) else ())).sum())
        chunks["allowed_principals"] = new
        report.append({"check": "chunk access list differs from ACL table", "rows_before": len(chunks),
                       "rows_after": len(chunks), "rows_affected": changed,
                       "action": "chunk list re-synced from doc_acl.csv"})
        print(f"{'chunk access list differs from ACL table':<45} affected: {changed}")

    # ---- save
    os.makedirs(a.out, exist_ok=True)
    users.to_csv(f"{a.out}/users.csv", index=False)
    groups.to_csv(f"{a.out}/groups.csv", index=False)
    members.to_csv(f"{a.out}/group_members.csv", index=False)
    docs.to_csv(f"{a.out}/documents.csv", index=False)
    acl.to_csv(f"{a.out}/doc_acl.csv", index=False)
    chunks.to_json(f"{a.out}/chunks.jsonl", orient="records", lines=True)
    ev = f"{a.inp}/revocation_events.csv"
    if os.path.exists(ev):                                   # keep only events that still make sense
        events = pd.read_csv(ev)
        events = events[events["user_id"].isin(users["user_id"]) & events["doc_id"].isin(docs["doc_id"])]
        events.to_csv(f"{a.out}/revocation_events.csv", index=False)
    pd.DataFrame(report).to_csv(f"{a.out}/cleaning_report.csv", index=False)
    print(f"\nclean data written to ./{a.out}/  (report: cleaning_report.csv)")
    print(f"users={len(users)} docs={len(docs)} chunks={len(chunks)} acl_rows={len(acl)}")


if __name__ == "__main__":
    main()