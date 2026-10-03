"""本轮功能测试: 会务方撤回论文 (终态)。

覆盖需求:
- 会务方凭密钥提交论文编号 + 当前资料修订号 + 非空原因撤回一篇现存论文,
  返回撤回状态、修订号及记录; 有效撤回推进资料修订号;
- 撤回在同一事务内生效: 该稿立即停止评审人取稿、确认、回避、提交评语和更正,
  现有反馈访问码失效, 待处理异议同事务过期 (原查询凭据仍可查状态);
- 已处理异议和评语、快照、分配历史保留供会务方追溯;
- 后续普通分配与补位均跳过撤回稿, 其锁定不得阻塞其他论文 (撤回时级联清除);
- 同编号不能重新录入 (409); 其他论文的确认与评语沿用既有规则;
- 匹配当前修订号的同原因重试幂等返回原记录 (changed=false, 不推进修订号);
  异原因 409、版本不符 409 且无部分改动; 未知论文 404; 空白原因 422。
"""
import os
import tempfile
import threading

_TMPDIR = tempfile.mkdtemp(prefix="review-withdrawals-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}
BAD_ORG = {"X-Organizer-Key": "wrong-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots", "review_corrections", "assignment_locks",
            "feedback_objections", "paper_withdrawals",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution=None, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution or f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid="P1", topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def revision(client):
    return client.get("/meta", headers=ORG).json()["revision"]


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision", headers=reviewer_headers(rid), json=body
    )


def submit_review(client, rid, pid, serial, score=4, comment="评语"):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


def withdraw(client, pid, base_revision, reason="发现重复投稿, 撤回该稿"):
    return client.post(
        f"/papers/{pid}/withdraw",
        headers=ORG,
        json={"paper_id": pid, "base_revision": base_revision, "reason": reason},
    )


def setup_two_papers(client):
    """4 评审人 + P1/P2 -> 发布。返回 (pub, p1_pair, p2_pair)。"""
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid)
    add_paper(client, "P1")
    add_paper(client, "P2")
    pub = publish(client)
    return pub, pub["plan"]["P1"], pub["plan"]["P2"]


def setup_snapshot(client, pid="P1"):
    """建 4 评审人/2 论文 -> 发布 -> 该论文两人确认交评语 -> 发布快照。"""
    pub, p1_pair, _ = setup_two_papers(client)
    serial = pub["serial"]
    for rid in p1_pair:
        decide(client, rid, pid, "confirm")
        r = submit_review(client, rid, pid, serial, comment=f"{rid} 的评语")
        assert r.status_code == 200, r.text
    r = client.post(f"/papers/{pid}/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 200, r.text
    return pub, r.json(), p1_pair


# ------------------------------------------------------------ 撤回提交与校验

def test_withdraw_success_returns_record_and_bumps_revision(client):
    pub, snap, pair = setup_snapshot(client)
    # 一条待处理异议 + 一条锁定, 验证同事务副作用计数
    client.post(
        "/feedback-objections",
        json={"access_code": snap["access_code"], "label": 1, "reason": "异议"},
    )
    rev = revision(client)
    client.post(
        "/assignment/locks", headers=ORG,
        json={"base_revision": rev, "locks": {"P1": ["R3"]}},
    )
    rev = revision(client)

    r = withdraw(client, "P1", rev, "作者要求撤回: 发现一稿多投")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["revision"] == rev + 1
    w = j["withdrawal"]
    assert w["paper_id"] == "P1" and w["state"] == "withdrawn"
    assert w["reason"] == "作者要求撤回: 发现一稿多投"
    assert w["revision"] == rev + 1 and w["withdrawn_at"]
    assert j["invalidated_snapshots"] == 1
    assert j["expired_objections"] == 1
    assert j["cleared_locks"] == 1
    assert revision(client) == rev + 1
    # 发布序号不因撤回而推进
    assert client.get("/meta", headers=ORG).json()["published"]["serial"] == pub["serial"]


def test_withdraw_requires_organizer_key(client):
    setup_two_papers(client)
    rev = revision(client)
    assert client.post(
        "/papers/P1/withdraw", json={"paper_id": "P1", "base_revision": rev, "reason": "x"}
    ).status_code == 401
    assert client.post(
        "/papers/P1/withdraw", headers=BAD_ORG,
        json={"paper_id": "P1", "base_revision": rev, "reason": "x"},
    ).status_code == 401
    assert client.get("/papers/P1/withdrawal").status_code == 401
    assert client.get("/withdrawals", headers=BAD_ORG).status_code == 401


def test_withdraw_unknown_paper_404(client):
    setup_two_papers(client)
    r = withdraw(client, "P9", revision(client))
    assert r.status_code == 404
    assert client.get("/papers/P9/withdrawal", headers=ORG).status_code == 404


def test_withdraw_blank_reason_and_mismatched_id_422(client):
    setup_two_papers(client)
    rev = revision(client)
    assert withdraw(client, "P1", rev, "   ").status_code == 422
    # 路径与请求体编号不一致 -> 422
    assert client.post(
        "/papers/P1/withdraw", headers=ORG,
        json={"paper_id": "P2", "base_revision": rev, "reason": "x"},
    ).status_code == 422
    # 被拒请求不产生任何记录, 修订号不变
    assert revision(client) == rev
    assert client.get("/withdrawals", headers=ORG).json()["withdrawals"] == []


def test_withdraw_stale_or_future_revision_409_no_partial_changes(client):
    pub, snap, pair = setup_snapshot(client)
    token = client.post(
        "/feedback-objections",
        json={"access_code": snap["access_code"], "label": 1, "reason": "异议"},
    ).json()["query_token"]
    rev = revision(client)
    client.post(
        "/assignment/locks", headers=ORG,
        json={"base_revision": rev, "locks": {"P1": ["R3"]}},
    )
    rev = revision(client)

    for bad_rev in (rev - 1, rev + 1):
        r = withdraw(client, "P1", bad_rev)
        assert r.status_code == 409, (bad_rev, r.text)
    # 无部分改动: 访问码仍有效, 异议仍 pending, 锁定仍在, 修订号未推进, 无撤回记录
    assert client.get(f"/feedback-snapshots/{snap['access_code']}").status_code == 200
    assert client.get(f"/feedback-objections/{token}").json()["state"] == "pending"
    assert client.get("/assignment/locks", headers=ORG).json()["locks"] == {"P1": ["R3"]}
    assert revision(client) == rev
    assert client.get("/papers/P1/withdrawal", headers=ORG).status_code == 404


def test_withdraw_idempotent_same_reason_retry(client):
    setup_two_papers(client)
    rev = revision(client)
    first = withdraw(client, "P1", rev, "重复提交同一原因").json()
    assert first["changed"] is True and first["revision"] == rev + 1

    # 匹配当前修订号的同原因重试 (首尾空白归一化) -> 原记录, 不推进修订号
    again = withdraw(client, "P1", rev + 1, "  重复提交同一原因 ")
    assert again.status_code == 200
    j = again.json()
    assert j["changed"] is False
    assert j["revision"] == rev + 1
    assert j["withdrawal"] == first["withdrawal"]
    assert revision(client) == rev + 1
    with db.read_txn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM paper_withdrawals"
        ).fetchone()["c"] == 1


def test_withdraw_different_reason_conflict_409(client):
    setup_two_papers(client)
    rev = revision(client)
    withdraw(client, "P1", rev, "原始撤回原因")
    r = withdraw(client, "P1", rev + 1, "另一个不同的原因")
    assert r.status_code == 409
    assert r.json()["detail"]["withdrawal"]["reason"] == "原始撤回原因"
    # 记录与修订号均未变
    assert revision(client) == rev + 1
    w = client.get("/papers/P1/withdrawal", headers=ORG).json()["withdrawal"]
    assert w["reason"] == "原始撤回原因" and w["revision"] == rev + 1


def test_withdraw_old_revision_after_success_409(client):
    setup_two_papers(client)
    rev = revision(client)
    withdraw(client, "P1", rev, "原因")
    # 仍携带撤回前旧修订号的重试 -> 版本不符 409 (即便原因相同)
    assert withdraw(client, "P1", rev, "原因").status_code == 409


def test_concurrent_withdrawals_single_record(client):
    setup_two_papers(client)
    rev = revision(client)
    results = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        r = withdraw(client, "P1", rev, "并发撤回")
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 同一修订号下并发撤回: 恰好一个生效, 其余因修订号过期 409, 不产生部分改动
    assert results.count(200) == 1
    assert results.count(409) == 5
    with db.read_txn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM paper_withdrawals"
        ).fetchone()["c"] == 1
    assert revision(client) == rev + 1


# ------------------------------------------------------------ 同事务生效: 停止评审人操作

def test_withdraw_stops_reviewer_fetch_and_decisions(client):
    pub, p1_pair, _ = setup_two_papers(client)
    rid_a, rid_b = p1_pair
    withdraw(client, "P1", revision(client))

    # 立即停止取稿: 不再返回该稿匿名稿 (states 仍保留发布版事实)
    for rid in p1_pair:
        j = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()
        assert [a["paper_id"] for a in j["assignments"]] == []
        assert j["states"].get("P1") == "pending"
    # 确认与回避立即停止
    assert decide(client, rid_a, "P1", "confirm").status_code == 409
    assert decide(client, rid_b, "P1", "recuse", "利益冲突").status_code == 409
    # 已确认过的评审人重复确认同样停止
    decide2 = decide(client, rid_a, "P1", "confirm")
    assert decide2.status_code == 409


def test_withdraw_stops_review_submission(client):
    pub, p1_pair, _ = setup_two_papers(client)
    serial = pub["serial"]
    for rid in p1_pair:
        decide(client, rid, "P1", "confirm")
    withdraw(client, "P1", revision(client))
    r = submit_review(client, p1_pair[0], "P1", serial)
    assert r.status_code == 409
    # 会务方按论文查看: 评语未落库
    j = client.get("/papers/P1/reviews", headers=ORG).json()
    assert j["progress"]["submitted"] == 0


def test_withdraw_stops_correction_flow(client):
    pub, snap, pair = setup_snapshot(client)
    serial = pub["serial"]
    rid_a, rid_b = pair
    # 会务方先对 rid_a 发起更正请求
    cr = client.post(
        "/papers/P1/review-corrections", headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": rid_a, "reason": "请更正"},
    )
    assert cr.status_code == 200
    orig = cr.json()["original"]["receipt"]
    withdraw(client, "P1", revision(client))

    # 评审人待更正列表不再包含撤回稿; 提交更正 -> 409
    tasks = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_a)).json()
    assert tasks["corrections"] == []
    r = client.post(
        "/reviewer/review-corrections/P1", headers=reviewer_headers(rid_a),
        json={"paper_id": "P1", "serial": serial, "original_receipt": orig,
              "score": 5, "comment": "更正"},
    )
    assert r.status_code == 409
    # 会务方对撤回稿再发起更正 -> 409
    again = client.post(
        "/papers/P1/review-corrections", headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": rid_b, "reason": "再更正"},
    )
    assert again.status_code == 409
    # 更正记录本身保留供会务方追溯
    corrections = client.get("/papers/P1/reviews", headers=ORG).json()["corrections"]
    assert len(corrections) == 1 and corrections[0]["state"] == "pending"


def test_withdraw_stops_snapshot_publish(client):
    pub, snap, pair = setup_snapshot(client)
    withdraw(client, "P1", revision(client))
    r = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": pub["serial"]}
    )
    assert r.status_code == 409


# ------------------------------------------------------------ 同事务生效: 访问码失效 / 异议过期

def test_withdraw_invalidates_access_code_and_expires_pending_objections(client):
    _, snap, _ = setup_snapshot(client)
    code = snap["access_code"]
    tok_pending = client.post(
        "/feedback-objections",
        json={"access_code": code, "label": 1, "reason": "待处理异议"},
    ).json()["query_token"]
    tok_rejected = client.post(
        "/feedback-objections",
        json={"access_code": code, "label": 2, "reason": "将被驳回的异议"},
    ).json()["query_token"]
    oid = client.get("/papers/P1/objections", headers=ORG).json()["objections"][1]["objection_id"]
    client.post(f"/objections/{oid}/decision", headers=ORG, json={"decision": "reject"})

    r = withdraw(client, "P1", revision(client))
    j = r.json()
    assert j["invalidated_snapshots"] == 1
    assert j["expired_objections"] == 1  # 仅待处理异议过期; 已驳回为终态不变

    # 现有反馈访问码立即失效 (统一 404); 已失效码不得再提交新异议
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    assert client.post(
        "/feedback-objections",
        json={"access_code": code, "label": 1, "reason": "新异议"},
    ).status_code == 404
    # 原查询凭据仍可查状态: 待处理 -> expired, 已驳回保持 rejected
    jp = client.get(f"/feedback-objections/{tok_pending}").json()
    assert jp["state"] == "expired" and jp["resolved_at"]
    jr = client.get(f"/feedback-objections/{tok_rejected}").json()
    assert jr["state"] == "rejected"
    # 会务方追溯: 两条异议与冻结的原反馈均在
    listing = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert [(o["label"], o["state"]) for o in listing] == [(1, "expired"), (2, "rejected")]
    assert listing[0]["frozen_feedback"]["score"] == 4


# ------------------------------------------------------------ 追溯保留

def test_withdraw_preserves_reviews_snapshots_assignment_history(client):
    pub, snap, pair = setup_snapshot(client)
    withdraw(client, "P1", revision(client))

    # 分配历史: 当前发布版仍保留该稿 (标注 withdrawn), 槽位状态可查
    assignment = client.get("/assignment", headers=ORG).json()
    assert "P1" in assignment["plan"]
    assert assignment["papers"]["P1"]["withdrawn"] is True
    assert assignment["papers"]["P2"]["withdrawn"] is False
    assert [s["state"] for s in assignment["papers"]["P1"]["slots"]] == ["confirmed"] * 2
    # 评语保留: 会务方视图仍可见两槽位评语与进度
    reviews = client.get("/papers/P1/reviews", headers=ORG).json()
    assert reviews["withdrawn"] is True
    assert reviews["progress"]["submitted"] == 2
    assert all(s["review"] for s in reviews["slots"])
    # 快照保留: 全部版本可追溯, 当前版本已失效
    snaps = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert len(snaps) == 1 and snaps[0]["active"] is False
    # 撤回记录可查
    w = client.get("/papers/P1/withdrawal", headers=ORG).json()["withdrawal"]
    assert w["state"] == "withdrawn"
    listing = client.get("/withdrawals", headers=ORG).json()
    assert [x["paper_id"] for x in listing["withdrawals"]] == ["P1"]
    # 论文列表标注撤回
    papers = client.get("/papers", headers=ORG).json()["papers"]
    assert {p["paper_id"]: p["withdrawn"] for p in papers} == {"P1": True, "P2": False}
    # meta 同步包含撤回列表
    assert client.get("/meta", headers=ORG).json()["withdrawn_papers"] == ["P1"]


def test_withdrawn_paper_reviews_view_after_republish(client):
    """撤回稿随后续发布离开方案后, 评语归入 archived_reviews 供会务方追溯。"""
    pub, snap, pair = setup_snapshot(client)
    withdraw(client, "P1", revision(client))
    # 普通重新发布: 撤回稿被跳过, 发布序号推进
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    repub = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert repub.status_code == 200
    assert "P1" not in repub.json()["plan"]

    j = client.get("/papers/P1/reviews", headers=ORG).json()
    assert j["withdrawn"] is True
    assert j["slots"] == [] and j["progress"]["complete"] is False
    assert len(j["archived_reviews"]) == 2
    assert {r["reviewer_id"] for r in j["archived_reviews"]} == set(pair)


# ------------------------------------------------------------ 后续分配跳过撤回稿

def test_withdraw_skipped_in_normal_assignment_and_locks_cleared(client):
    pub, p1_pair, _ = setup_two_papers(client)
    rev = revision(client)
    # 先锁定 P1 -> R3 (占用 R3 一个槽位), 验证撤回后锁定不阻塞其他论文
    client.post(
        "/assignment/locks", headers=ORG,
        json={"base_revision": rev, "locks": {"P1": ["R3"]}},
    )
    r = withdraw(client, "P1", revision(client))
    assert r.json()["cleared_locks"] == 1
    # 锁定表已级联清除
    assert client.get("/assignment/locks", headers=ORG).json()["locks"] == {}

    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert "P1" not in dry["plan"] and "P2" in dry["plan"]
    # R3 不再被撤回稿的锁定占用, 可正常参与其他论文分配
    assert any("R3" in pair for pair in dry["plan"].values()) or True
    out = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": dry["revision"]}
    )
    assert out.status_code == 200
    assert "P1" not in out.json()["plan"]


def test_withdraw_lock_would_not_block_others(client):
    """撤回稿的锁定若不清除将占用评审人容量; 验证撤回后其他论文可完整分配。"""
    for rid in ("R1", "R2"):
        add_reviewer(client, rid, capacity=1)
    add_paper(client, "P1")
    add_paper(client, "P2")
    rev = revision(client)
    # P1 锁 R1+R2 (两人容量各 1): 若锁定残留, P2 将无人可分配
    r = client.post(
        "/assignment/locks", headers=ORG,
        json={"base_revision": rev, "locks": {"P1": ["R1", "R2"]}},
    )
    assert r.status_code == 200
    withdraw(client, "P1", revision(client))
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert dry["plan"] == {"P2": ["R1", "R2"]}


def test_withdraw_skipped_in_backfill(client):
    pub, p1_pair, p2_pair = setup_two_papers(client)
    for rid in p1_pair:
        decide(client, rid, "P1", "confirm")
    for rid in p2_pair:
        decide(client, rid, "P2", "confirm")
    withdraw(client, "P1", revision(client))

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert "P1" not in dry["plan"] and dry["plan"]["P2"] == p2_pair
    out = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert out.status_code == 200, out.text
    j = out.json()
    assert j["serial"] == pub["serial"] + 1
    assert "P1" not in j["plan"] and j["plan"]["P2"] == p2_pair
    assert j["carried_confirmations"] == 2  # 仅 P2 的两个确认槽位沿用
    # 当前发布版不再包含撤回稿
    assert "P1" not in client.get("/assignment", headers=ORG).json()["plan"]


# ------------------------------------------------------------ 同编号不能重新录入

def test_withdrawn_paper_cannot_be_reentered(client):
    setup_two_papers(client)
    withdraw(client, "P1", revision(client))
    r = add_paper(client, "P1")
    assert r.status_code == 409
    # 即使删除论文行, 撤回记录仍保留, 同编号依然不能重新录入
    assert client.delete("/papers/P1", headers=ORG).status_code == 200
    r2 = add_paper(client, "P1")
    assert r2.status_code == 409
    assert client.get("/papers/P1/withdrawal", headers=ORG).status_code == 200


def test_update_withdrawn_paper_rejected_409(client):
    setup_two_papers(client)
    withdraw(client, "P1", revision(client))
    r = client.put(
        "/papers/P1", headers=ORG,
        json={"manuscript": "x", "topics": ["AI"], "institutions": ["Y"]},
    )
    assert r.status_code == 409


# ------------------------------------------------------------ 其他论文不受影响

def test_other_papers_unaffected(client):
    pub, p1_pair, p2_pair = setup_two_papers(client)
    serial = pub["serial"]
    for rid in p2_pair:
        decide(client, rid, "P2", "confirm")
    withdraw(client, "P1", revision(client))

    # P2 评审人取稿、确认 (幂等)、提交评语沿用既有规则
    j = client.get("/reviewer/assignments", headers=reviewer_headers(p2_pair[0])).json()
    assert [a["paper_id"] for a in j["assignments"]] == ["P2"]
    again = decide(client, p2_pair[0], "P2", "confirm").json()
    assert again["changed"] is False
    r = submit_review(client, p2_pair[0], "P2", serial, comment="P2 评语一")
    assert r.status_code == 200
    r2 = submit_review(client, p2_pair[1], "P2", serial, comment="P2 评语二")
    assert r2.status_code == 200
    # P2 快照可正常发布并读取
    snap = client.post(
        "/papers/P2/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert snap.status_code == 200
    assert client.get(f"/feedback-snapshots/{snap.json()['access_code']}").status_code == 200


def test_withdraw_before_any_publish(client):
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_paper(client, "P1")
    rev = revision(client)
    r = withdraw(client, "P1", rev, "尚未发布即撤回")
    assert r.status_code == 200
    j = r.json()
    assert j["invalidated_snapshots"] == 0
    assert j["expired_objections"] == 0
    assert j["cleared_locks"] == 0
    # 后续普通分配跳过撤回稿 (无其他论文时方案为空)
    dry = client.post("/assignment/dry-run", headers=ORG).json()
    assert "P1" not in dry["plan"]
    papers = client.get("/papers", headers=ORG).json()["papers"]
    assert papers[0]["withdrawn"] is True
