# rclone serve s3 のユーザーメタデータを Drive に保存する（Phase 2）設計

- 日付: 2026-10-08
- 状態: 設計（2026-10-08 ユーザー承認済み）。
- 根拠: [Phase 2 の調査](../../../rclone_dir_cache/2026-10-08/phase2-metadata-investigation-ja.md)
- 対象: rclone `tongsama/rclone` の `1.75.1-improve-kaz`（dd03d0243、Phase 1 を含む）から作業ブランチを切る。
- 前提となる Phase 1: [パス指定 lookup の設計](2026-10-08-rclone-lookup-by-path-design.md)

## 1. 目的と前提

**構成**: 複数のホストが、それぞれ `rclone serve s3 gdrive_kwatan:/rclone-s3 --kaz-vfs-lookup-by-path --no-cleanup --vfs-cache-mode off ...` を動かし、同じ Drive フォルダを共有する。

**今の問題**（調査で確認）:
- serve s3 は S3 のユーザーメタデータを、プロセスのメモリ `b.meta` にだけ置いている。JuiceFS が PUT で送るチェックサム（`X-Amz-Meta-Crc32c`）は、PUT したホストでしか、しかも再起動するまでしか返らない。他のホストの GET や再起動後の GET では、JuiceFS のチェックサム検証が黙って省略される。
- `b.meta` は、他のホストが消した key の分が残り、メモリが増え続ける。リクエストの付随ヘッダ（`X-Amz-Date` など）まで入っている。

**目的**:
- `X-Amz-Meta-*`（すべて。ユーザー判断）を Drive の object の properties に保存し、どのホストからでも、再起動後も HEAD・GET で返す。
- このとき `b.meta` を使わない。
- PUT・HEAD・GET の Drive API の回数を増やさない。

**守ること**:
- オプションを指定しないときは、今とまったく同じ動作にする。
- 保存の失敗やメタデータの取得の失敗を隠さない（黙って捨てない）。
- メタデータが無い時間を作らない（本体と同時に書く）。
- 既存の、メタデータの無い object をそのまま扱える。

**範囲外**:
- 既存の object への後付け（crc32c を計算し直すには全 object を読む必要がある）。
- `--vfs-cache-mode` が `off` 以外のときの保存。
- 同じ key でメタデータだけを置き換える Copy。

## 2. オプション（ユーザー承認済み）

| オプション | 種類 | 既定 | 内容 |
|---|---|---|---|
| `--kaz-s3-persist-metadata` | serve s3 の全体オプション（Groups に `Kaz`、ヘルプの先頭に `[kaz] `） | false | `X-Amz-Meta-*` をメモリではなく backend の object に保存し、HEAD・GET でそこから返す |
| `--drive-kaz-properties` | Drive backend のオプション（config 名 `kaz_properties`。`rclone.conf` にも書ける。ヘルプの先頭に `[kaz] `） | false | 一覧・NewObject・アップロードの応答で Drive の `properties` も取得して保持し、ユーザーメタデータとして返す。上書き時に、今回のメタデータに無い既存の properties を消す |

命名規則の補足（ユーザー承認）: backend のオプションは rclone が backend 名を先頭に付けるので、`--<backend>-kaz-<内容>` とする。

本番では両方を指定する。`--drive-kaz-properties` が無くても動くが、HEAD・GET で object ごとに API が1回増える。

## 3. 保存するものとキーの対応づけ（第1節）

- 保存するのは、名前が `X-Amz-Meta-` で始まるヘッダだけ（大文字小文字は区別しない）。`X-Amz-Date`、`Content-Md5`、受信時刻の `Last-Modified` などは保存しない。
- 保存: `X-Amz-Meta-<Name>` → Drive の property `s3m-<name を小文字にしたもの>`。値はそのまま。
  - 接頭辞は、rclone の Drive backend が特別に扱う名前（`mtime`、`btime`、`content-type`、`description` など、backend/drive/metadata.go:541-617）とぶつけないため。
  - 小文字にするのは、S3 のメタデータ名が大文字小文字を区別しないため。
- 読み出し: `s3m-<name>` → `X-Amz-Meta-<name>`（net/http がヘッダ名を正規化する）。`s3m-` で始まらない property は返さない。
- HEAD・GET の `Content-Type` は Drive の mimeType から、`Last-Modified` は Drive の modifiedTime から作る（メモリの値で上書きしない）。
- 上限（[Drive 公式](https://developers.google.com/drive/properties)）: 1件はキーと値を合わせて UTF-8 で124バイト、1ファイルあたり public な properties は30個。超えたときは Drive API のエラーで PUT を失敗させ、黙って捨てない。超えたときに本体が作られないかを実機で確かめ、中途半端に作られるなら、serve s3 側で事前に数えて 400 `MetadataTooLarge` を返す。
- `X-Amz-Meta-Mtime`（と `mtime`）で Drive の更新時刻を設定する今の動作は残す。値は `s3m-mtime` としても保存される。

## 4. 書き込み（第2節）

### 4.1 serve s3（`--kaz-s3-persist-metadata` のとき）

- PutObject: §3 の変換をしたメタデータを、VFS の書き込みハンドルに渡す（§4.2）。`b.meta` には入れない。
- multipart: CreateMultipartUpload のメタデータを、完了時の最終ファイルの書き込みに渡す。serve s3 は一時的な名前で書いて正式な名前に移動する（multipart.go:173-175, 412-415）ので、移動後も properties が残るかを実機で確かめる。
- CopyObject（別の key）: 元を読んで PutObject する今の経路で、メタデータが付く。
- CopyObject（同じ key でメタデータだけ置き換え）: 501 `NotImplemented` を返す。
- 起動時に `--vfs-cache-mode` が `off` でなければエラーで終了する（キャッシュ経由の書き戻しには、メタデータを渡す経路が無いため）。
- DELETE は `b.meta` に触れない（使っていないため）。
- オプションが off のときは、`b.meta` を使う今の動作のまま。

### 4.2 VFS

- cache-mode off の書き込みハンドル（`WriteFileHandle`）に、アップロード時のメタデータを設定するメソッドを足す。最初の書き込み（`openPending`）より前に呼ぶ。呼ばれたのが遅ければエラーを返す。
- メタデータが設定されていれば、`operations.Rcat` に渡し、そのハンドル専用に「メタデータを送る」設定（`ci.Metadata = true`、`fs.AddConfig` で作る）を有効にする。設定しなければ今と同じ。
- serve s3 は、`vfs.Create` が返したハンドルがこのメソッドを持つかを型で確かめて呼ぶ。持たなければエラー（PUT を失敗させる）。

### 4.3 Drive（`--drive-kaz-properties`）

- アップロード（create・update・resumable）の応答の fields に `properties` を加え、PUT 直後の object がユーザーメタデータを持つようにする（drive.go:2593、4506、upload.go:57）。
- 上書き（既存の object の Update）では、今回のメタデータに無い既存の properties を削除する（Drive API に null を送る）。S3 の PUT はメタデータを丸ごと置き換えるため。既存の properties は、Put が先に行う `NewObject` で取得済みなので、API は増えない。Go の Drive クライアントで map の1項目に null を送る方法（`NullFields`）は実装時に確かめる。
- `ci.Metadata`（`--metadata`）が true のときは、rclone の既存の動作を優先する。

## 5. 読み出し（第3節）

- `--drive-kaz-properties` のとき、一覧・NewObject・アップロードの応答の fields に `properties` だけを加える（`permissions`・`owners` などは取らない）。object はユーザー properties を保持し、`Metadata(ctx)` には追加の API 無しでそれを返す（`ci.Metadata` が false のとき）。
- serve s3 は、HEAD・GET のときに object の `fs.Metadataer.Metadata` から `s3m-*` を取り出して返す。この呼び出しは「メタデータを読む」設定の ctx で行う（`--drive-kaz-properties` が無い backend でも、空の結果をキャッシュしないため）。
  - 取得がエラーなら 500 を返す。
  - 自ホストでアップロード中で object がまだ無い場合は、ユーザーメタデータ無しで返す。
  - backend が `fs.Metadataer` を持たない場合は、ユーザーメタデータ無しで返す（エラーにはしない）。
- lookup モードのエントリも、一覧のエントリも、同じ経路で properties を持つ。

## 6. 互換性と、入れ替え途中の状態

- 改修前の object（properties 無し）は、ユーザーメタデータ無しとして返る。JuiceFS は今と同じく検証を省略する。
- 1台ずつ入れ替える途中で新旧の版が混ざっても安全: 古い版は properties を読み書きしない。新しい版は、古い版が書いた object をメタデータ無しとして扱う。
- 例外: 古い版のホストが、既存の object を違う中身で上書きすると、古い crc32c が残る（古い版は不要になったキーを消さない）。JuiceFS は同じ key を違う中身で書き直さないので、実際には起きないと考える。

## 7. テスト

### 7.1 単体テスト（ローカルで完結、TDD）

- VFS: 書き込みハンドルに設定したメタデータが Put に届く（Put の引数を記録するテスト用の Fs）。設定しなければ届かない。最初の書き込みの後に設定するとエラー。
- serve s3（properties を記録する共有のテスト用 backend。2つの serve を同じ backend につなぐと、別のホストや再起動に相当する）:
  - A で PUT した `X-Amz-Meta-Crc32c` が、B の HEAD・GET で返る。`b.meta` は空のまま。
  - 違うメタデータで上書きすると、古いキーが返らない。
  - オプション off では今と同じ（`b.meta` を使う）。
  - cache-mode が `off` 以外なら起動時にエラー。同じ key へのメタデータだけの Copy は 501。
  - メタデータの取得がエラーなら 500。
  - **既存データとの互換（ユーザー要望）**: メタデータの無い object を backend に直接置き、(1) HEAD・GET で正常に取得できる（メタデータ無し、中身は正しい）、(2) メタデータ付きで上書きでき、その後の HEAD・GET でメタデータと新しい中身が返る、(3) DELETE で消せる、(4) その後の HEAD・GET は 404。
- Drive: properties の fields への追加と、上書き時の null の送り方は、可能な範囲で単体テストにし、残りは実機で確かめる。

### 7.2 実際の Drive での確認

テスト用フォルダ（例 `gdrive_kwatan:/rclone-s3-test`）を使い、作成と削除はユーザーの確認を取る。本番の `/rclone-s3` と本番の rclone には触らない。

- 改修版の serve s3 を2つ（両方のオプションを指定）立て、A で PUT したメタデータが B と、再起動後の A から HEAD・GET で返る。
- 違うメタデータで上書きすると、古いキーが消える。
- 上限を超えるメタデータの PUT は失敗し、object が作られない（作られる場合は §3 のとおり事前の確認を加える）。
- multipart で、移動の後も properties が残る。
- PUT・HEAD・GET の Drive API の回数が、Phase 1 と同じ。
- **既存データとの互換**: 改修前の `/usr/bin/rclone`（v1.75.1）でテスト用フォルダに object を直接書き、改修版の serve s3 で §7.1 の (1)〜(4) を行う。
- 余力があれば、テスト用フォルダの上で JuiceFS を動かし、チェックサムの検証が効いていることを確かめる。

### 7.3 本番への適用

ユーザーが行う。Phase 1 の手順書に、`--kaz-s3-persist-metadata` と `rclone.conf` の `kaz_properties = true`（または `--drive-kaz-properties`）を加える。1台ずつ入れ替えてよい（§6）。
