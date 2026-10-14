// Command orphancache classifies locally cached JuiceFS blocks by whether their
// slice ID is still referenced in Redis metadata. It only issues read commands
// (SCAN, LRANGE, HSCAN) and never writes to Redis or the cache directory.
package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// cacheEntry is one cached block file parsed from "<size> <mtime> <id>_<indx>_<len>".
type cacheEntry struct {
	id    uint64
	bytes int64
	mtime float64
}

// loadCacheList reads the pre-captured cache listing and returns its entries.
func loadCacheList(path string) []cacheEntry {
	f, err := os.Open(path)
	if err != nil {
		panic(err)
	}
	defer f.Close()
	var out []cacheEntry
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fs := strings.Fields(sc.Text())
		if len(fs) != 3 {
			continue
		}
		sz, _ := strconv.ParseInt(fs[0], 10, 64)
		mt, _ := strconv.ParseFloat(fs[1], 64)
		id, err := strconv.ParseUint(strings.SplitN(fs[2], "_", 2)[0], 10, 64)
		if err != nil {
			continue
		}
		out = append(out, cacheEntry{id, sz, mt})
	}
	return out
}

// main scans chunk lists, sliceRef and delSlices, then prints per-class totals.
func main() {
	ctx := context.Background()
	opt, err := redis.ParseURL(os.Getenv("META_URL"))
	if err != nil {
		panic(err)
	}
	rdb := redis.NewClient(opt)
	entries := loadCacheList(os.Args[1])
	want := map[uint64]bool{}
	for _, e := range entries {
		want[e.id] = true
	}

	start := time.Now()
	live := map[uint64]bool{}
	var cursor uint64
	nkeys := 0
	for {
		keys, next, err := rdb.Scan(ctx, cursor, "c*", 1000).Result()
		if err != nil {
			panic(err)
		}
		// LRANGE each chunk key in one pipeline per SCAN batch.
		pipe := rdb.Pipeline()
		cmds := make([]*redis.StringSliceCmd, 0, len(keys))
		for _, k := range keys {
			if !strings.Contains(k, "_") {
				continue
			}
			cmds = append(cmds, pipe.LRange(ctx, k, 0, -1))
		}
		if len(cmds) > 0 {
			if _, err := pipe.Exec(ctx); err != nil && err != redis.Nil {
				panic(err)
			}
		}
		for _, c := range cmds {
			nkeys++
			for _, v := range c.Val() {
				b := []byte(v)
				for i := 0; i+24 <= len(b); i += 24 {
					// slice record: pos u32, id u64, size u32, off u32, len u32 (big endian)
					id := binary.BigEndian.Uint64(b[i+4 : i+12])
					if id != 0 {
						live[id] = true
					}
				}
			}
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}

	// sliceRef: k<id>_<size> -> refs-1; negative values are durable dead markers.
	refVal := map[uint64]string{}
	cursor = 0
	for {
		kv, next, err := rdb.HScan(ctx, "sliceRef", cursor, "", 1000).Result()
		if err != nil {
			panic(err)
		}
		for i := 0; i+1 < len(kv); i += 2 {
			ps := strings.SplitN(strings.TrimPrefix(kv[i], "k"), "_", 2)
			id, _ := strconv.ParseUint(ps[0], 10, 64)
			if want[id] {
				refVal[id] = kv[i+1]
			}
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}

	// delSlices: <id>_<ts> -> concatenated (id u64, size u32) of delayed slices.
	delayed := map[uint64]bool{}
	cursor = 0
	for {
		kv, next, err := rdb.HScan(ctx, "delSlices", cursor, "", 1000).Result()
		if err != nil {
			panic(err)
		}
		for i := 1; i < len(kv); i += 2 {
			b := []byte(kv[i])
			for j := 0; j+12 <= len(b); j += 12 {
				delayed[binary.BigEndian.Uint64(b[j:j+8])] = true
			}
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	elapsed := time.Since(start)

	type agg struct {
		files int
		bytes int64
		ids   map[uint64]bool
	}
	classes := map[string]*agg{}
	add := func(c string, e cacheEntry) {
		a := classes[c]
		if a == nil {
			a = &agg{ids: map[uint64]bool{}}
			classes[c] = a
		}
		a.files++
		a.bytes += e.bytes
		a.ids[e.id] = true
	}
	orphanFile, _ := os.Create(os.Args[2])
	defer orphanFile.Close()
	for _, e := range entries {
		v, hasRef := refVal[e.id]
		switch {
		case live[e.id]:
			add("live", e)
		case hasRef && strings.HasPrefix(v, "-"):
			add("dead_marker(sliceRef<0)", e)
		case delayed[e.id]:
			add("delayed(delSlices)", e)
		default:
			add("unreferenced", e)
			fmt.Fprintf(orphanFile, "%d %d %.0f ref=%q\n", e.id, e.bytes, e.mtime, v)
		}
	}
	fmt.Printf("chunk keys scanned=%d live slice ids=%d delayed ids=%d elapsed=%s\n", nkeys, len(live), len(delayed), elapsed)
	for c, a := range classes {
		fmt.Printf("%-26s files=%5d slices=%5d bytes=%.3f GiB\n", c, a.files, len(a.ids), float64(a.bytes)/(1<<30))
	}
}
