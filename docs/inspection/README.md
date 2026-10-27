# 調査の結果と証跡

JuiceFS 本体（`juicefs/`）と rclone 本体（`rclone/`）以外の、調査の報告・解析スクリプト・ログの抜粋・集計を置く場所です。各テーマのディレクトリの下に、日付ごとのディレクトリ（`<テーマ>/<YYYY-MM-DD>/`）があります。

確定した知見の要約は [findings](../findings.md)、計画と仕様は [superpowers](../superpowers/README.md)、今の作業は [TODO](../../TODO.md) を参照してください。ここにある報告は、当時の記録です。

2026-10-13 に、ルートにあった各テーマのディレクトリをここへ移しました（ディレクトリ名は変えていません）。過去の記録（`agent_memo.md` など）に出てくる `gc_backlog/…` のようなパスは、`docs/inspection/gc_backlog/…` と読み替えてください。

| テーマ | 日付 | 内容 | 入口 |
|---|---|---|---|
| initial_incident | 2026-10-01 | 最初の VM I/O 障害の証拠と、当初の調査状況の引き継ぎ | [引き継ぎ](initial_incident/juicefs_vm_corruption_handoff_codex.md) |
| small_put_investigation | 2026-10-02 | 小さい PUT の raw／payload／freeze の初期調査 | [freeze の報告](small_put_investigation/2026-10-02/freeze.report.md) |
| incident_recurrence | 2026-10-02 | 障害の再発、FUSE watchdog、待機の経路 | [ディレクトリ](incident_recurrence/2026-10-02/) |
| combined_vm_io | 2026-10-02 | FUSE・GC・scheduler・reuse の統合版のビルドとテストの記録 | [ディレクトリ](combined_vm_io/2026-10-02/) |
| restart_check | 2026-10-02 | ゲストの再起動と I/O の遅延 | [待ちの報告](restart_check/2026-10-02/wait.report.md) |
| gc_backlog | 2026-10-02 | staging の滞留、local retirement、回帰の検証 | [ディレクトリ](gc_backlog/2026-10-02/) |
| option_effects | 2026-10-03 | オプションの導入効果、明示的 flush、健全性、ログの容量 | [総合報告](option_effects/2026-10-03/report-ja.txt) |
| rclone_put_timeout | 2026-10-06 | rclone serve s3 の PUT の詰まり | [報告](rclone_put_timeout/2026-10-06/report-ja.md) |
| metadata_random_io | 2026-10-07 | 巨大ファイルの random I/O の metadata path（Ubuntu インストールの計測） | [計測](metadata_random_io/2026-10-07/install-report-ja.md)、[range](metadata_random_io/2026-10-07/install-range-report-ja.md)、[batch](metadata_random_io/2026-10-07/install-batch-report-ja.md) |
| rclone_dir_cache | 2026-10-08 | rclone serve s3 の dir cache と複数ホスト、Drive での確認、本番適用の手順 | [本番適用の手順](rclone_dir_cache/2026-10-08/deploy-runbook-ja.md) |
| juicefs_gc | 2026-10-08 | `juicefs gc` が object の一覧の 30s で止まる件 | [調査報告](juicefs_gc/2026-10-08/gc-list-timeout-investigation-ja.md) |
| vm_boot_cold_read | 2026-10-08〜09 | キャッシュが空のときの VM 起動、GET の改修の計測、header-timeout による EIO、Drive の遅さの時間帯 | [報告](vm_boot_cold_read/2026-10-08/report-ja.md)、[時間帯の集計](vm_boot_cold_read/2026-10-09/drive-latency-by-hour-ja.md) |
| rclone_v1752_port | 2026-10-13 | rclone の kaz 改修を公式 v1.75.2 へ移植するときの衝突・意味の衝突・推奨手順、移植版の実 Drive での確認 | [調査報告](rclone_v1752_port/2026-10-13/port-investigation-ja.md)、[Drive での確認](rclone_v1752_port/2026-10-13/drive-verification-ja.md) |
| vm_write_hotspot | 2026-10-13〜15 | VM（qcow2 preallocation=metadata、unsafe）の起動とアプリでの freeze の内訳、書き込みの偏り（journald）、compaction の書き直し、ブロックサイズの見積もり、read cache に残る slice。kaz compaction policy の実機計測（PUT の総数、cooldown の削除、upload-delay）、ブロック単位の compaction の見積もりと実機計測（§6〜11） | [報告](vm_write_hotspot/2026-10-13/report-ja.md)、[policy と block の計測](vm_write_hotspot/2026-10-14/report-ja.md)、[idle 31s の見積もり](vm_write_hotspot/2026-10-15/idle31-estimate-ja.md)、[raw と qcow2 の比較](vm_write_hotspot/2026-10-15/raw-vs-qcow2-ja.md) |
| cache_churn | 2026-10-19〜20 | 長時間の稼働でキャッシュが入れ替わる件。metrics の定期取得、キャッシュの中身の生死の内訳、kaz-2q の実機の判定、夜間の compaction の量の解析 | [報告](cache_churn/2026-10-19/report-ja.md)、[夜間の compaction](cache_churn/2026-10-20/compaction-overnight-ja.md) |
| delta_slice | 2026-10-20 | 差分 slice 方式（同じ chunk の小さい書き込みを 1 つの slice に集め、複数の record で参照する compaction）の実現可能性。参照数、読み込み、compaction、gc、改造していない client との互換、壊れ方の一覧（ソースの読み取りのみ） | [調査報告](delta_slice/2026-10-20/feasibility-ja.md) |
| kaz_measure_replay_suite | 2026-10-21 | 計測と流し直しの suite（`tests/juicefs/`）。本番の計測、write の記録と試験の volume への流し直し、compaction の設定 6 つを 1 枚の表で比べる | [README](../../tests/juicefs/README-ja.md)、[設計](../superpowers/specs/2026-10-21-kaz-measure-replay-suite-design.md) |
| upstream_v1.4.2 | 2026-10-26 | kaz の改修を upstream v1.4.2 へ載せるときの衝突、upstream を採る重複の修正、意味の衝突（format の atomic 化）、今後取り込みやすくする改善提案 | [調査報告](upstream_v1.4.2/2026-10-26/port-investigation-ja.md) |
| doc_migration_2026-10-03 | 2026-10-03 | 調査文書を本体リポジトリから分離したときの移設と確認の記録 | [ディレクトリ](doc_migration_2026-10-03/) |
