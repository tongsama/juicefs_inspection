# 計画・仕様の一覧

このディレクトリは、別管理の JuiceFS 本体を調査・改善した際の計画と仕様を保存します。既存文書を `juicefs/docs/superpowers/` から移設しました。各文書の進捗欄は当時の記録であり、現在の残課題は [TODO](../../TODO.md)、知見は [findings](../findings.md) を優先します。

| フェーズ | 仕様・設計根拠 | 実装計画 |
|---|---|---|
| 初期 VM I/O 障害・Read flush エラー | [初期引き継ぎ](../../juicefs_vm_corruption_handoff_codex.md) | [2026-10-01 調査・修正](plans/2026-10-01-vm-io-investigation.md) |
| FUSE 待機・GC 分離・背景 scheduler・slice 再利用 | [統合仕様](specs/2026-10-02-vm-io-combined.md) | [統合計画](plans/2026-10-02-vm-io-combined.md) |
| 不要 staging の早期回収と upload 競合 | [local retirement 仕様](specs/2026-10-02-vm-io-gc-retirement.md) | [local retirement 計画](plans/2026-10-02-vm-io-gc-retirement.md) |
| 改修版バイナリの配布（Releases・install スクリプト） | [配布仕様](specs/2026-10-04-release-distribution.md) | [配布計画](plans/2026-10-04-release-distribution.md) |
| 巨大ファイル random I/O の metadata path 最適化（調査・計画。Phase 1 は実装済み） | [調査結果と実装計画](specs/2026-10-07-metadata-random-io-optimization-plan.md) | 同左（Phase 0〜4） |
| 同 chunk の slice commit をまとめる（Phase 2、`--meta-write-batch`、実装済み・計測待ち） | [設計](specs/2026-10-07-meta-write-batch-design.md) | [実装計画](plans/2026-10-07-meta-write-batch.md) |
| rclone serve s3 のパス指定 lookup（rclone Phase 1、`--kaz-vfs-lookup-by-path`、実装・実機確認済み、merge 済み） | [設計](specs/2026-10-08-rclone-lookup-by-path-design.md) | [実装計画](plans/2026-10-08-rclone-lookup-by-path.md) |
| rclone serve s3 のユーザーメタデータを Drive に保存（rclone Phase 2、`--kaz-s3-persist-metadata`・`--drive-kaz-properties`、実装・実機確認済み、merge 済み） | [設計](specs/2026-10-08-rclone-s3-persist-metadata-design.md) | [実装計画](plans/2026-10-08-rclone-s3-persist-metadata.md) |
| 配布を JuiceFS と rclone の2製品に対応（Phase 3、製品別タグ・共通 install スクリプト、実装中） | [設計](specs/2026-10-08-release-multi-product-design.md) | [実装計画](plans/2026-10-08-release-multi-product.md) |

文書中の `pkg/`・`cmd/` と Go コマンドは、本体の `juicefs/` を基準にします。ソースを本体から移したものではなく、調査文書の管理先だけを分離しています。
