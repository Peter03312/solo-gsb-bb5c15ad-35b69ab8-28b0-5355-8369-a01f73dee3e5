#!/usr/bin/env bash
# 本轮用法端到端示例: 会务方撤回论文 (终态)
#   发布 -> 确认/评语/快照/异议/锁定 -> 会务方凭密钥+当前修订号+非空原因撤回 P1 ->
#   同一事务内生效: 停止取稿/确认/回避/评语/更正, 访问码失效, 待处理异议过期,
#   锁定级联清除 -> 同原因幂等/异原因 409/版本不符 409/未知论文 404 ->
#   同编号不能重新录入 -> 后续普通分配与补位跳过撤回稿 -> 会务方追溯记录
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/withdrawals.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 2 篇论文 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
for P in P1 P2; do
  curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"paper_id\": \"$P\", \"manuscript\": \"$P 匿名稿全文...\", \"topics\": [\"AI\"],
    \"institutions\": [\"Univ-$P\"]}"
done

echo "== 2. 预演 + 发布, 两篇论文的评审人均确认并各交一份正式评语 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SER=$(echo "$PUB" | j 'd["serial"]')
echo "当前发布序号 serial=$SER, 方案: $(echo "$PUB" | j 'd["plan"]')"
for P in P1 P2; do
  RA=$(echo "$PUB" | j "d[\"plan\"][\"$P\"][0]")
  RB=$(echo "$PUB" | j "d[\"plan\"][\"$P\"][1]")
  for H in "$RA r$(echo "$RA" | sed 's/R//')-secret" "$RB r$(echo "$RB" | sed 's/R//')-secret"; do
    set -- $H
    curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$P/decision" \
      -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
      -H 'Content-Type: application/json' -d "{\"paper_id\": \"$P\", \"decision\": \"confirm\"}"
  done
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$P/review" \
    -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: r$(echo "$RA" | sed 's/R//')-secret" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\": \"$P\", \"serial\": $SER, \"score\": 4, \"comment\": \"$P 评语一: 选题重要。\"}"
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$P/review" \
    -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: r$(echo "$RB" | sed 's/R//')-secret" \
    -H 'Content-Type: application/json' \
    -d "{\"paper_id\": \"$P\", \"serial\": $SER, \"score\": 2, \"comment\": \"$P 评语二: 实验不足。\"}"
done

echo "== 3. 为 P1 发布反馈快照 + 作者提交一条待处理异议 + 会务方锁定 P1->R3 =="
SNAP1=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE1=$(echo "$SNAP1" | j 'd["access_code"]')
echo "P1 access_code=$CODE1"
TOKEN=$(curl -s -X POST "$BASE/feedback-objections" -H 'Content-Type: application/json' \
  -d "{\"access_code\": \"$CODE1\", \"label\": 1,
       \"reason\": \"评语一引用的对比实验并非本文方法, 请核查\"}" | j 'd["query_token"]')
echo "P1 待处理异议查询凭据: $TOKEN"
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/assignment/locks" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R3\"]}}" \
  | j '(d["changed"], d["locks"])'

echo "== 4. 会务方凭密钥 + 当前资料修订号 + 非空原因撤回 P1 (同一事务内生效) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
WD=$(curl -s -X POST "$BASE/papers/P1/withdraw" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV,
       \"reason\": \"作者通知一稿多投, 会务方决定撤回该稿\"}")
echo "$WD" | python3 -m json.tool
N_INV=$(echo "$WD" | j 'd["invalidated_snapshots"]')
N_EXP=$(echo "$WD" | j 'd["expired_objections"]')
N_LOCK=$(echo "$WD" | j 'd["cleared_locks"]')
echo "-> 撤回状态/修订号/记录已返回; 同事务内: 访问码失效 $N_INV 版, 待处理异议过期 $N_EXP 条, 锁定清除 $N_LOCK 行"

echo "== 5. 同事务生效验证: 访问码失效 / 异议过期(凭据仍可查) / 锁定清除 / 停止评审人操作 =="
curl -s -o /dev/null -w 'P1 旧访问码读取        -> HTTP %{http_code} (404, 已失效)\n' \
  "$BASE/feedback-snapshots/$CODE1"
echo -n '异议凭据状态              -> '; curl -s "$BASE/feedback-objections/$TOKEN" | j 'd["state"]'
echo -n '锁定表(撤回稿级联清除)    -> '; curl -s "$BASE/assignment/locks" -H "$ORG" | j 'd["locks"]'
R1A=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
R1C="r$(echo "$R1A" | sed 's/R//')-secret"
echo -n '评审人取稿(不再含 P1)     -> '
curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $R1A" -H "X-Reviewer-Credential: $R1C" \
  | j '[a["paper_id"] for a in d["assignments"]]'
curl -s -o /dev/null -w '对该稿确认/回避          -> HTTP %{http_code} (409, 已停止)\n' \
  -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $R1A" -H "X-Reviewer-Credential: $R1C" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -w '对该稿提交评语          -> HTTP %{http_code} (409, 已停止)\n' \
  -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $R1A" -H "X-Reviewer-Credential: $R1C" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 5, \"comment\": \"试图提交\"}"
curl -s -o /dev/null -w '对该稿发起更正/发布快照 -> HTTP %{http_code} (409, 已停止)\n' \
  -X POST "$BASE/papers/P1/review-corrections" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"reviewer_id\": \"$R1A\", \"reason\": \"试图更正\"}"

echo "== 6. 幂等与冲突: 同原因重试返原记录(不推进修订号) / 异原因 409 / 版本不符 409 / 未知论文 404 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P1/withdraw" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV,
       \"reason\": \"作者通知一稿多投, 会务方决定撤回该稿\"}" \
  | j '(d["changed"], d["revision"], d["withdrawal"]["reason"])'
curl -s -o /dev/null -w '异原因重复撤回          -> HTTP %{http_code} (409, 无部分改动)\n' \
  -X POST "$BASE/papers/P1/withdraw" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV, \"reason\": \"另一个原因\"}"
curl -s -o /dev/null -w '旧修订号撤回            -> HTTP %{http_code} (409, 版本不符)\n' \
  -X POST "$BASE/papers/P1/withdraw" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $((REV - 1)), \"reason\": \"作者通知一稿多投, 会务方决定撤回该稿\"}"
curl -s -o /dev/null -w '未知论文撤回            -> HTTP %{http_code} (404)\n' \
  -X POST "$BASE/papers/P9/withdraw" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P9\", \"base_revision\": $REV, \"reason\": \"x\"}"
curl -s -o /dev/null -w '空白原因                -> HTTP %{http_code} (422)\n' \
  -X POST "$BASE/papers/P1/withdraw" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"base_revision\": $REV, \"reason\": \"   \"}"

echo "== 7. 同编号不能重新录入 (409) =="
curl -s -o /dev/null -w '重新录入 P1             -> HTTP %{http_code} (409)\n' \
  -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "manuscript": "新稿", "topics": ["AI"], "institutions": ["Univ-Z"]}'

echo "== 8. 其他论文沿用既有规则: P2 取稿/评语/快照不受影响 =="
R2A=$(echo "$PUB" | j 'd["plan"]["P2"][0]')
echo -n 'P2 评审人取稿             -> '
curl -s "$BASE/reviewer/assignments" -H "X-Reviewer-Id: $R2A" \
  -H "X-Reviewer-Credential: r$(echo "$R2A" | sed 's/R//')-secret" \
  | j '[a["paper_id"] for a in d["assignments"]]'
SNAP2=$(curl -s -X POST "$BASE/papers/P2/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE2=$(echo "$SNAP2" | j 'd["access_code"]')
curl -s -o /dev/null -w 'P2 快照持码读取         -> HTTP %{http_code} (200, 不受影响)\n' \
  "$BASE/feedback-snapshots/$CODE2"

echo "== 9. 后续普通分配与补位均跳过撤回稿 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB2=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
echo "重新发布后方案: $(echo "$PUB2" | j 'd["plan"]') (不含 P1)"

echo "== 10. 会务方追溯: 撤回记录 / 评语 / 快照 / 异议历史均保留 =="
echo -n '撤回记录                  -> '
curl -s "$BASE/papers/P1/withdrawal" -H "$ORG" | j 'd["withdrawal"]'
echo -n '全部撤回记录              -> '
curl -s "$BASE/withdrawals" -H "$ORG" | j '[w["paper_id"] for w in d["withdrawals"]]'
echo '撤回稿评语(离开方案后归入 archived_reviews):'
curl -s "$BASE/papers/P1/reviews" -H "$ORG" \
  | j '(d["withdrawn"], [r["receipt"] for r in d["archived_reviews"]])'
echo -n '快照版本(已失效, 可追溯)  -> '
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  | j '[(s["version"], s["active"]) for s in d["snapshots"]]'
echo -n '异议历史(含已过期)        -> '
curl -s "$BASE/papers/P1/objections" -H "$ORG" \
  | j '[(o["label"], o["state"]) for o in d["objections"]]'
echo "完成。"
