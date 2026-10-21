# 計測の suite と流し直しの試験の suite 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本番にも試験にも使える計測の CLI（`jfsmeasure`）と、本番の書き込みを記録して試験の volume（一時 Redis＋RTT 10 ms、本物の Drive）へ流し直し、compaction の設定を 10〜15 分ずつ比べる試験の CLI（`jfsreplay`）を作る。

**Architecture:**
- 道具は全部 Python 3 の標準ライブラリだけで書く。置き場所は `tests/juicefs/` で、`lib/`（共通）、`measure/`、`replay/` に分ける。
- 製品のコード（`juicefs/`）は変えない。試験は、本番と同じオプションの別の `juicefs mount` を外から操作する。
- 計測は、どちらのプロファイル（prod、test）でも同じ記録の形（`metrics.tsv` など）に書く。試験の結果も、同じ解析のコードで作る。

**Tech Stack:** Python 3.14（`python3 -I`）、標準ライブラリの `unittest`、bash（起動の補助だけ）、`juicefs`（試験のバイナリ）、redis-server 8.0.5（scratch に展開済みのもの）、`rclone serve s3`（本番のもの、試験用のバケット）。

**Spec:** `docs/superpowers/specs/2026-10-21-kaz-measure-replay-suite-design.md`（ユーザー承認）。実装する人は、この計画と設計書の両方を読む。

## Global Constraints

- 置き場所は `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/`。調査のリポジトリの中に置く。初期の untracked のファイルなので、add と commit はユーザーの許可があるときだけ行う。
- 本番の metadata、object storage、キャッシュ、staging、元のログを変えない。本番の Redis には、読み取りの命令（`LLEN`、`LRANGE`、`HGET`、`DBSIZE`、`SCAN` だけ）しか送らない。
- 接続の URL、鍵、パスワードは、表示しない。ファイル、ログ、例外の文言、`run.json` にも書かない。次の場所から実行時に読む。
  - Redis: `~/utilities/cloudfs/juicefs_mount.sh` の `MOUNT_FROM`
  - rclone: `~/utilities/cloudfs/rclone_s3_start.sh` の `RCLONE_SERVE_S3_ACCESS_KEY`、`RCLONE_SERVE_S3_SECRET`、`RCLONE_SERVE_S3_PORT`
- 固定値:

  | 項目 | 値 |
  |---|---|
  | 本番の metrics | `http://127.0.0.1:9567/metrics` |
  | 本番の VM のディスク | inode 634040 |
  | 本番のキャッシュ | `~/.juicefs/cache/73e74f7b-6f95-4155-adea-d9ef1e669c20` |
  | 本番の mountpoint | `~/mnt_juicefs` |
  | 本番のバケット | `buckets` |
  | 試験の Redis | 127.0.0.1:16379 |
  | RTT の中継 | 127.0.0.1:16380、片道 5 ms |
  | 試験の metrics | 127.0.0.1:9568 |
  | 試験のバケット | `juicefs-kaz-test` |
  | 試験の mountpoint | `~/mnt_juicefs_test` |
  | 試験のキャッシュ | `~/.juicefs/cache-test` |
  | 試験のバイナリの既定 | `~/.local/bin/kaz-test/juicefs-delta-7336466c` |
  | 種の上限 | 32 GiB |
  | 落ち着きの判定 | 60 秒、上限 10 分 |
  | 取りこぼしの許容 | 1% |

- 間隔: metrics 60 秒、LLEN 600 秒、キャッシュの出入り 300 秒。
- 新しい関数とクラスには、目的が分かる日本語のコメント（docstring）を付ける（今の調査の道具と同じ書き方）。表示とエラーの文言も日本語。
- 観測、source、推論、未検証を、報告の表で分ける（AGENTS.md）。
- テストは `cd tests/juicefs && python3 -I -m unittest discover -s . -p 'test_*.py'`。Drive に触れるテストは無し。Drive を使うのは、ユーザーが始める本番の試験の実行だけ。

## Review Focus

1. **安全の確かめが、設定の書き間違いで外れないこと。** 例: 試験のプロファイルの Redis の host を本番と同じにする、バケット名を `buckets` にする、mountpoint を本番と同じにする。どれも書き込みの前に止まること。→ Task 5 の `test_safety_refuses_*`
2. **`.accesslog` の行の取りこぼしや、知らない形の行。** 知らない形の行は数えて報告し、捨てた数が 0 でなければ記録の報告に出ること。→ Task 4 の `test_parse_unknown_lines_counted`
3. **mount のし直しで counter が 0 に戻る区間。** 差が負にならず、足し合わせになること。→ Task 2 の `test_counter_reset_summed`
4. **流し直しの途中で mount が落ちた、または EIO が返った場合。** その回を「失敗」として表に出し、次の設定へ進まずに止まること。本番の再現の試験で、失敗を隠さないため。→ Task 7 の `test_replay_stops_on_eio`
5. **記録の時刻が前後している行（複数の thread）。** 開始の時刻で並べ直し、pid ごとの順序は保つこと。→ Task 4 の `test_trace_sorted_by_start_keeps_pid_order`

---

### Task 1: 共通の部品（`lib/`）

**Files:**
- Create: `tests/juicefs/lib/__init__.py`、`lib/config.py`、`lib/secrets.py`、`lib/resp.py`、`lib/metrics_io.py`
- Test: `tests/juicefs/test_lib.py`

**Interfaces（Produces）:**
- `config.load_profile(name: str) -> Profile`
  - Profile は dataclass。項目は `name`、`metrics_url`、`inode`、`cache_dir`、`mountpoint`、`redis_source`（"mount_script" か "test"）、`redis_addr`、`log_glob`。
  - 値は `tests/juicefs/profiles.toml`（Task 1 で作る。上の固定値を書く）から読む。
- `secrets.redis_url_from_mount_script(path) -> str` と `secrets.rclone_keys(path) -> tuple[str, str, int]`
  - 戻り値を repr や str で表示しないため、`Secret` という小さな型で包む。`str()` は `<redacted>` を返し、本当の値は `.value` でだけ取れる。
- `resp.Resp(host, port, password=None, db=0, readonly=True)`
  - `call(*args)`、`send(*cmds)`、`read()` を持つ（今の `cache_breakdown.py` から移す）。
  - readonly のときは、許可した命令（Global Constraints の一覧）以外を送ろうとすると `PermissionError`。
- `metrics_io`:
  - `append_sample(path, ts, series: dict[str, float])`
  - `load(path) -> dict[str, list[tuple[int, float]]]`（gzip 可、時刻で並べ替える）
  - `delta(series, a, b) -> float`（counter の 0 への戻りを足し合わせる。窓の始まりは a 以後の最初のサンプル）
  - `scrape(url, include: re.Pattern) -> dict`（共通のラベル instance、juicefs_version、mp、vol_name を外す）

- [ ] **Step 1: テストを書く**
  - `test_secret_never_prints`: `str(Secret("x"))` と `repr` が `<redacted>`。`f"{s}"` も同じ。
  - `test_resp_readonly_blocks_write`: readonly の Resp に `SET` を送ると `PermissionError`。接続は偽の socket（`socket.socketpair`）。
  - `test_counter_reset_summed`: 系列 [(0,10),(60,30),(120,5),(180,15)] の 0〜180 の delta が 35。
  - `test_window_start_first_after`: 窓の始まりより前に、前の mount の大きい値がある系列で、delta が窓の中の増分だけ。
  - `test_load_sorts_unsorted_append`: 時刻が前後して追記された TSV を、並べ替えて読む。
- [ ] **Step 2: 失敗を確かめる** — `python3 -I -m unittest test_lib -v` → FAIL（module が無い）
- [ ] **Step 3: 実装する**（Interfaces のとおり。`scrape` の対象の系列は、2026-10-21 版の `metrics_sampler.sh` の正規表現と同じ）
- [ ] **Step 4: 通ることを確かめる** — 同じコマンドで PASS
- [ ] **Step 5: Commit**（許可がある場合）: `git add tests/juicefs/lib tests/juicefs/profiles.toml tests/juicefs/test_lib.py && git commit -m "tests: add common parts of the juicefs measure and replay suites"`

---

### Task 2: 計測の常駐と `start`／`check`／`stop`／`capture`

**Files:**
- Create: `tests/juicefs/jfsmeasure`（実行ファイル。`measure/cli.py` を呼ぶ）、`measure/__init__.py`、`measure/cli.py`、`measure/samplers.py`、`measure/capture.py`
- Test: `tests/juicefs/test_measure_run.py`

**Interfaces:**
- Consumes: Task 1 の全部
- Produces:
  - 出力ディレクトリの中身: `run.json`、`metrics.tsv`、`llen.tsv`、`keys-snapshot-<UNIX>.tsv`、`keys-events.tsv`、`samplers.log`
  - `run.json` のキー: `profile`、`started`（UNIX）、`binary`（path、version、sha256）、`kaz_log_lines`、`pids`（{"metrics","llen","keys"}）、`intervals`
  - `samplers.py` の 3 つの常駐の入口（`python3 -I -m measure.samplers metrics|llen|keys <dir>`）。`setsid` で切り離して起動する。今の `metrics_sampler.sh`（10-21 版）、`llen_sampler.py`、`key_tracker.py` の記録の形をそのまま使う。

- [ ] **Step 1: テストを書く**（本番に繋がない。偽の metrics の HTTP サーバー（`http.server` を thread で）、一時ディレクトリのキャッシュ、偽の Resp を使う）
  - `test_start_writes_run_json_and_pids`: `start test <dir>` で 3 つの常駐が起動し、run.json に PID と binary の sha256 がある。stop で全部止まる。
  - `test_check_reports_missing_sampler`: 常駐の 1 つを kill すると、`check` が日本語で足りないものを出し、0 以外で終わる。
  - `test_check_stale_record`: metrics.tsv の最後の更新が 3 間隔より古いと、`check` が警告する。
  - `test_capture_files`: `capture <dir> pre-reboot` で `metrics-pre-reboot-*.txt`、`breakdown-pre-reboot-*.txt`、`files-pre-reboot-*.csv`、`state-pre-reboot-*.v1` ができる。
  - `test_start_refuses_existing_run`: 動いている run.json がある出力ディレクトリへの `start` は止まる。
- [ ] **Step 2: 失敗を確かめる** — `python3 -I -m unittest test_measure_run -v` → FAIL
- [ ] **Step 3: 実装する**
  - `capture` のキャッシュの内訳は、`cache_breakdown.py` を移して使う。分類のために本番の Redis を読むのは、許可した読み取りの命令だけ。
  - バイナリの version は `<binary> version` から取る。本番の場合、バイナリの path は mount の process の `/proc/<pid>/exe` から取る。
  - mount の設定は `<mountpoint>/.config`（JSON）を読み、接続先・鍵らしい項目（キーの名前に key、secret、pass、token、addr、url、bucket を含むもの、値に `://` を含むもの）を `<redacted>` にして `config.json` に書く。コマンドラインからは推さない。テスト `test_start_copies_config_redacted` を足す（偽の .config から、伏せた値と残す値を確かめる）。
  - `compare`（Task 3）は、区間ごとの `config.json` の違う項目を、表の先頭に出す。
- [ ] **Step 4: 通ることを確かめる**
- [ ] **Step 5: Commit**（許可がある場合）: `tests/juicefs/jfsmeasure tests/juicefs/measure tests/juicefs/test_measure_run.py`、メッセージは "tests: add measurement samplers and capture"

---

### Task 3: 解析（`progress`、`compare`、`boot`）

**Files:**
- Create: `tests/juicefs/measure/report.py`（表を作る部分）、`measure/boot.py`
- Modify: `measure/cli.py`（3 つの sub-command）
- Test: `tests/juicefs/test_measure_report.py`、`tests/juicefs/testdata/`（小さい metrics.tsv、keys-*.tsv の作り物）

**Interfaces:**
- `report.progress(dir, start, end) -> str`。項目は次のとおりで、各項目に系列の名前を脚注で付ける。
  - `[流入]`、`[読み込み]`、`[2Q]`、`[期間の終わりの状態]`: 今の `progress_kaz.sh` と同じ並び・同じ言葉。
  - `[compaction]`
    - delta と block の結果の event ごとの数
    - 書き直しの理由ごとのブロックの数（`delta_blocks_{whole,fallback,nobase,ratio}`、`delta_fold_blocks`）
    - 段のバイト、まとめ直しのバイト、外した record の数
    - staging 1 GiB あたりの compaction の出力
  - `[object storage]`: PUT・GET・DELETE の回数とバイト、平均の所要時間、PUT の回数と PUT のバイトの staging 1 GiB あたりの値
  - `[片付け]`: `compaction_gc_events_total{event="local_retire_success"}` など
  - `[メタデータ]`: meta ops と transaction の数、平均の所要時間
  - `[record の数]`: llen.tsv の区間の最後の行
- `progress` の既定は「開始から今まで」と「直近 1 時間」の 2 区間を続けて出す。
- `report.compare(specs: list[str]) -> str`: 今の `compare_runs.py` の表に、上の compaction、片付け、メタデータの行を足す。区間の指定は `ラベル:ディレクトリかTSV:開始:終了`（`first`／`now` 可）。
- `boot.analyze(dir, start, end, marks: dict[str,str] | None) -> str`: 今の `boot_analysis.py` と同じ判定。marks はユーザーの 4 つの時刻（起動開始、ログイン画面、ログイン完了、アプリ）で、経過の分を表に出す。

- [ ] **Step 1: テストを書く**
  - `test_progress_sections`: 作り物の metrics.tsv から、全部の見出しと、期待する数字（例: staging 1.00 GiB、compaction 0.56 GiB、比 0.56）が出る。
  - `test_progress_matches_old_script`: 同じ作り物に今の `progress_kaz.sh` をかけた出力と、`[流入]` から `[期間の終わりの状態]` までの数字が同じ。
  - `test_compare_reads_old_gz`: `docs/inspection/cache_churn/2026-10-19/metrics.tsv.gz` の 10/19 21:38〜06:05 で、staging 5.00、compaction 32.65、PUT 7.69 GiB／138,163 回、DELETE 193,226 回が出る（前回の報告の値）。
  - `test_boot_marks_minutes`: marks から、起動の開始からの分（1.3、1.8、4.5 など）が表に出る。
- [ ] **Step 2: 失敗を確かめる**
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 通ることを確かめる**
- [ ] **Step 5: Commit**（許可がある場合）: "tests: add progress, compare and boot reports"

---

### Task 4: 書き込みの記録（`jfsreplay record`、`verify`）

**Files:**
- Create: `tests/juicefs/jfsreplay`（実行ファイル）、`replay/__init__.py`、`replay/cli.py`、`replay/accesslog.py`、`replay/trace.py`
- Test: `tests/juicefs/test_trace.py`、`tests/juicefs/testdata/accesslog-sample.txt`（10/14 の accesslog の先頭 200 行と、手で作った setattr／fallocate／`: (n)` の行）

**Interfaces:**
- `accesslog.parse_line(line, inode) -> Op | None | Unknown`
  - Op は dataclass。項目は `start_us`（終わった時刻 − 所要時間）、`end_us`、`pid`、`kind`、`off`、`size`、`mode`、`dur_us`。
  - kind は write、read、fsync、flush、fallocate、truncate のどれか。
  - 対象の inode 以外は None。知らない形は Unknown。
- `jfsreplay record <out.tsv> --inode 634040 [--mountpoint ~/mnt_juicefs]`
  - `.accesslog` を開いて読み続け、Op だけを TSV に追記する。
  - Ctrl-C か `stop` で止まる。止まるときに、Unknown の数と行の総数を `<out>.meta.json` に書く。
  - 4 つの時刻は `jfsreplay mark <out.tsv> <ラベル>` で、今の時刻を `<out>.marks.tsv` に足す。
- `jfsreplay verify <out.tsv> <計測の出力ディレクトリ>`
  - 記録の write のバイトと回数を、同じ区間の `staging_write_bytes` の差と比べ、差の割合を出す。
  - 差が 1% を超えたら、0 以外で終わる。
- `trace.load(path) -> list[Op]`: 開始の時刻で並べる。同じ pid の中の順序は記録の順を保つ。

- [ ] **Step 1: テストを書く**
  - `test_parse_write_read_fallocate_setattr`: 各 kind の行から、正しい off、size、mode、start_us が出る（write は `(ino,size,off,fh)`、fallocate は `(ino,mode,off,size)`）。
  - `test_parse_read_both_suffixes`: `: n` と `: (n)` の両方を読める。
  - `test_parse_unknown_lines_counted`（Review Focus 2）: 形の違う行が Unknown になり、meta.json の数に入る。
  - `test_trace_sorted_by_start_keeps_pid_order`（Review Focus 5）
  - `test_verify_threshold`: 差 0.5% は 0 で終わり、差 2% は 1 で終わる。
- [ ] **Step 2: 失敗を確かめる**
- [ ] **Step 3: 実装する**
  - 記録の常駐は、行を読んだらすぐ絞り込んで書く（buffer の 10,240 行があふれないように）。
  - 開始時に、本番の mount の `.accesslog` が読めることを確かめる。
- [ ] **Step 4: 通ることを確かめる**
- [ ] **Step 5: Commit**（許可がある場合）: "tests: record and verify accesslog traces"

---

### Task 5: 試験の volume（RTT の中継、一時 Redis、format／mount）

**Files:**
- Create: `tests/juicefs/replay/rtt_proxy.py`、`replay/testvol.py`、`tests/juicefs/mount_flags.txt`（本番のスクリプトの `JUICEFS_CUSTOM_OPTIONS` と、mount の共通のオプションを写したもの。compaction の行に印を付ける）
- Modify: `replay/cli.py`（`testvol up|down|status|destroy`）
- Test: `tests/juicefs/test_testvol.py`

**Interfaces:**
- `rtt_proxy.serve(listen, target, one_way_ms)`: asyncio の TCP の中継。往路と復路それぞれ、受け取った塊を `one_way_ms` だけ遅らせて同じ順で送る。`python3 -I -m replay.rtt_proxy` で常駐できる。
- `testvol.up(profile, settings)`。手順:
  1. redis-server（scratch の展開物）を 16379 で起動する。
  2. 中継を 16380 で起動する。
  3. 試験のバケットが無ければ作る。S3 の CreateBucket を、標準ライブラリの urllib と SigV4 の署名で送る。
  4. volume が無ければ `juicefs format` する。block size と圧縮は、本番の `juicefs status` の値（読み取りのみ）、trash-days は 0。
  5. `mount_flags.txt` と settings の compaction の値で、`juicefs mount --background`（metrics 9568、試験の cache と mountpoint）する。
- `testvol.down()`: umount、中継の停止。Redis は残す。`destroy` は、確認の入力（`destroy` と打つ）の後に、試験のバケットの中身と Redis を消す。
- `testvol.safety_check(profile)`。次のどれかなら、`SafetyError` で止まる。
  - Redis の host と port が本番と同じ
  - バケットが `buckets`
  - mountpoint、キャッシュ、metrics の port のどれかが本番と同じ

- [ ] **Step 1: テストを書く**
  - `test_rtt_proxy_adds_delay`: 手元の echo サーバーに中継を通すと、往復が 10 ms ± 2 ms 増える。pipeline で 50 個の命令を送っても、往復 1 回分しか増えない。
  - `test_safety_refuses_prod_redis`、`test_safety_refuses_prod_bucket`、`test_safety_refuses_prod_mountpoint`（Review Focus 1）
  - `test_mount_flags_override_compaction`: settings の値で、`mount_flags.txt` の compaction の行だけが置き換わる。ほかの行は同じ。
  - `test_mount_flags_no_stray_args`: 作った引数の列に、空白だけの要素や `\` が無い（今回の mount スクリプトの件の再発防止）。
  - `test_up_down_local_storage`（結合。`--storage file` の一時ディレクトリと試験のバイナリで、実際に format・mount・書き込み・umount）。バイナリか FUSE が無い環境では skip。Drive は使わない。
- [ ] **Step 2: 失敗を確かめる**
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 通ることを確かめる**
- [ ] **Step 5: Commit**（許可がある場合）: "tests: add the test volume with an RTT proxy"

---

### Task 6: 種のファイル（`jfsreplay seed`）

**Files:**
- Create: `tests/juicefs/replay/seed.py`
- Modify: `replay/cli.py`
- Test: `tests/juicefs/test_seed.py`

**Interfaces:**
- `seed.chunks(trace: list[Op]) -> list[int]`: write と read が触れた 64 MiB の chunk の番号。
- `seed.content(off: int, n: int) -> bytes`: 位置から決まり、圧縮がよく効くバイト列。例: 4 KiB ごとに位置の値を 1 回だけ書き、残りを 0 にする。
- `jfsreplay seed <trace.tsv> [--limit 32G]`
  - 試験の mount（block モードの設定で up）に、`/seed/<trace の名前>.img` を VM のディスクと同じ長さで作る（ftruncate）。
  - chunks の範囲だけを書き、fsync する。
  - `juicefs compact` をかけ、chunk がブロックの形になったことを、試験の Redis の LRANGE で確かめる。
  - 上限を超えたら、書く前に止まる。

- [ ] **Step 1: テストを書く**
  - `test_chunks_from_trace`: 境目をまたぐ write が 2 つの chunk になる。read だけの chunk も入る。
  - `test_content_deterministic_compressible`: 同じ位置で同じ中身になる。zlib で 1/50 以下に縮む。
  - `test_seed_limit_refuses`
  - `test_seed_local_storage`（結合。Task 5 の local の volume で小さい trace の種を作り、record がブロックの形）
- [ ] **Step 2〜4:** 失敗を確かめる → 実装する → 通ることを確かめる
- [ ] **Step 5: Commit**（許可がある場合）: "tests: build seed files for replay"

---

### Task 7: 1 回の試験（`jfsreplay run`）

**Files:**
- Create: `tests/juicefs/replay/player.py`、`replay/run.py`
- Modify: `replay/cli.py`
- Test: `tests/juicefs/test_player.py`

**Interfaces:**
- `player.play(path, ops, t0) -> PlayResult`
  - pid ごとに thread を作り、各 Op を `t0 + (start_us − 最初の start_us)` の時刻に発行する。write、read、fsync、fallocate（ctypes）、ftruncate を使う。flush は最後の close で代える。
  - 中身は `seed.content` と同じ作り方で、位置の値に 1 を足したもの（種と違う中身にする）。
  - PlayResult は次の項目を持つ。
    - kind ごとの待ち時間の中央値・99%・最大
    - 予定の時刻からの遅れの最大
    - エラーの一覧
  - EIO などのエラーが出たら、その時点で止める（Review Focus 4）。
- `jfsreplay run <trace.tsv> --settings <名前> [--age N] [--out results/<日時>/<名前>]`。手順は設計書 §5.4 の 1〜8 のとおり。
  - 落ち着いたかは、試験の metrics で判定する。`staging_blocks` が 0、`object_request_uploading` が 0、`compact_size_histogram_bytes_count` が 60 秒変わらない状態。
  - 結果は `<out>/run.json`、`metrics.tsv` など（計測の suite の形）と `play.json`。
  - Drive の rate limit（`object_request_errors` の増加、ログの 429／403 の rate limit）が出たら止める。

- [ ] **Step 1: テストを書く**
  - `test_play_timing`: 一時ファイル（FUSE でない）に 100 個の Op を流し、予定の時刻からの遅れが 20 ms 以内。
  - `test_play_pid_threads`: 2 つの pid の Op が並行に発行される（片方の遅い fsync で、もう片方が止まらない）。
  - `test_replay_stops_on_eio`: EIO を返す偽のファイル（monkeypatch の os.pwrite）で、失敗として止まり、エラーが結果に出る。
  - `test_run_local_storage`（結合。local の volume、小さい trace、`--settings default`、短い落ち着きの判定）: run.json、metrics.tsv、play.json ができる。clone が消える。
- [ ] **Step 2〜4:** 失敗を確かめる → 実装する → 通ることを確かめる
- [ ] **Step 5: Commit**（許可がある場合）: "tests: run one replay against the test volume"

---

### Task 8: 設定の組と比較の表（`jfsreplay suite`）、README

**Files:**
- Create: `tests/juicefs/settings.toml`（設計書 §5.5 の 6 つ）、`replay/suite.py`、`tests/juicefs/README-ja.md`
- Modify: `replay/cli.py`、`docs/inspection/README.md`（調査の一覧に「計測と試験の suite」を足す）
- Test: `tests/juicefs/test_suite.py`

**Interfaces:**
- `settings.toml` の各項目: `name`、`mode`（delta／block）、`flags`（例 `{"kaz-compaction-delta-trigger" = 30}`）
- `jfsreplay suite <trace.tsv> [--only a,b] [--repeat 2] [--age N]`
  - 設定を順に `run` する。
  - 最後に `results/<日時>/summary.md` を作る。表は `report.compare` と同じ行で、列は設定ごと。play の待ち時間も含め、各行の出どころの系列を脚注に書く。
  - 途中の失敗は、その設定を「失敗」として表に出し、そこで止める。
- README-ja.md には次を書く。
  - 本番の計測の手順（start → check → capture → boot → compare）
  - 試験の手順（record → mark → verify → seed → suite）
  - `mount_flags.txt` を本番のスクリプトから写して確かめる手順
  - 安全の約束

- [ ] **Step 1: テストを書く**
  - `test_settings_default_six`: 既定の 6 つが、設計書の名前と値で読める。
  - `test_summary_table_columns`: 2 つの作り物の結果から、列が 2 つ、行に staging 1 GiB あたりの PUT と compaction の出力がある表になる。
  - `test_suite_stops_on_failure`: 2 つ目の設定が失敗すると、3 つ目は流さず、表に失敗が出る。
- [ ] **Step 2〜4:** 失敗を確かめる → 実装する → 通ることを確かめる
- [ ] **Step 5: 全体の確認**
  - `python3 -I -m unittest discover -s tests/juicefs -p 'test_*.py' -v` が全部 PASS（FUSE と バイナリが要る結合のテストを含む）。
  - `jfsmeasure --help`、`jfsreplay --help` が日本語で出る。
- [ ] **Step 6: Commit**（許可がある場合）: "tests: add the replay suite runner and README"

## 計画の外（ユーザーと一緒に行うこと）

1. 本番での記録: ユーザーの合図で `jfsmeasure start prod` と `jfsreplay record` を始める。VM を再起動し、4 つの時刻を `mark` で入れ、10〜15 分で止める。その後 `verify` で取りこぼしを確かめる。
2. 本物の Drive での種の作成と、6 つの設定の試験（1 回 15 分、約 1.5 時間）。始める前に、試験用のバケットを作ってよいかをユーザーに確かめる。
