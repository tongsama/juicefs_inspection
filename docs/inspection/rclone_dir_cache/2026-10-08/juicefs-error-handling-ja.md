# JuiceFS の S3 エラー処理の確認（404 / 500 / 503）

## 目的

rclone serve s3 の変更（Stat が「存在しない」以外で失敗したとき、404 NoSuchKey/NoSuchBucket ではなく 500 InternalError を返す）が、JuiceFS の S3 クライアントと chunk store の挙動に問題を起こさないかを、JuiceFS ソースで確かめる。特に、404・500・503(SlowDown) で再試行や待機を変えているかを見る。

## 観測（file:line）

対象: `juicefs/` のソース（読み取りのみ）。

- `pkg/object/s3.go:587` `options.RetryMaxAttempts = 1`。AWS SDK 側の再試行は無効（1 回のみ試行）。
- `pkg/object/s3.go:99-117` `Head`: `*types.NotFound`（HEAD の 404）のときだけ `os.ErrNotExist` に変換。それ以外のエラーは素通し。
- `pkg/object/s3.go:122-` `Get`: エラーはリクエスト ID を取り出して返すのみ。404 と 5xx で分岐しない。
- `pkg/object/s3.go:229` `Delete`: エラー文字列に `NoSuchKey` が含まれていれば成功扱い（nil）。DELETE の 404 だけを特別扱い。
- `pkg/object/restful.go:247-249` `Head`: 404 のとき `os.ErrNotExist`。他の非 200 は `parseError`（`restful.go:239` 付近で `status: %v, message: ...`）。（S3 バックエンド `s3.go` とは別の汎用 REST クライアント）
- `pkg/chunk/cached_store.go:752-762,773` `loadRange`: Get が失敗すると（エラーの種類を問わず）`errTryFullRead` を返し、全体読みにフォールバック。
- `pkg/chunk/cached_store.go:810-830` `load`: Get の失敗は `fmt.Errorf("get %s: %s", key, err)` に包んで返すのみ。コメントに "it will be retried in the upper layer."（`cached_store.go:809`）。404 と 5xx の区別なし。
- `pkg/chunk/cached_store.go:158,170` `rSlice.ReadAt`: `errTryFullRead` 以外はそのまま上位へ返す。
- `pkg/vfs/reader.go:155,186,226` 読み出しの再試行: `trycnt > maxRetries`（`conf.Meta.Retries`）までを `retry_time(trycnt)` の待機つきで繰り返す。エラーの種類による分岐は確認できなかった。
- `pkg/chunk/cached_store.go:375-390` `upload`: PUT は `max=3`（非同期）または `MaxRetries+1`（sync、既定 `MaxRetries=10`、`cached_store.go:852-853`）回、`try*try` 秒の待機つきで再試行。分岐はエラーの種類によらない（`store.put` の戻りが nil かどうかだけ）。
- 404・500・503 を区別する分岐を `cached_store.go` 内で grep（`NoSuchKey|StatusNotFound|IsNotExist|SlowDown|StatusServiceUnavailable`）しても、該当は無かった。`SlowDown` の文字列による待機の分岐は `s3.go`・`restful.go`・`cached_store.go` のいずれにも無い。

## 結論

- 観測の範囲では、JuiceFS のデータ読み書き経路（chunk store）は Get/Put の失敗を、404・500・503 の別なく同じ再試行（GET は上位の `retry_time`、PUT は `try*try` 秒待機）で扱う。503 SlowDown だけ待機を変える処理は見つからなかった。
- 区別があるのは `Head`（404 を ErrNotExist に変換）と `Delete`（NoSuchKey を成功扱い）のみ。rclone serve s3 の今回の変更は、`Stat` が「存在しない」以外で失敗した場合のみ 500 に変えるもので、真に存在しないオブジェクトは従来どおり 404 のままなので、これらの分岐には影響しない。
- したがって、Drive のレート制限などで 404 の代わりに 500 を返しても、JuiceFS は「存在しない」と誤認せず再試行側に倒れる。503 SlowDown を返す追加タスクは不要で、「500 のままで十分」と判断する。
- 注意: 404 を 500 にしたことで、GET が一時的に失敗したときの待機は PUT/GET 共通の既存の再試行間隔に従う。

## 未確認

- 実環境（JuiceFS 本体から rclone serve s3 へ接続）での 500 時の挙動は実測していない。ソース上の読み取りのみ。
- AWS SDK 内部（smithy）が 5xx に対して再試行以外の処理（例: クライアント側のレート制御）を行うかは、`RetryMaxAttempts=1` により事実上無効と見ているが SDK ソースは未確認。
- メタデータ側の `conf.Meta.Retries` の既定値と `retry_time` の具体的な待機値は未確認。
- `pkg/vfs/reader.go` の再試行がエラー種別を一切見ないことは、該当行付近の grep の範囲での確認であり、全分岐を読み切ってはいない。
