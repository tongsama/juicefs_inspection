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
| doc_migration_2026-10-03 | 2026-10-03 | 調査文書を本体リポジトリから分離したときの移設と確認の記録 | [ディレクトリ](doc_migration_2026-10-03/) |
