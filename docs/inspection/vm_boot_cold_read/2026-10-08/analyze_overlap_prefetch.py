"""QEMU の遅い read と重ならない GET の数（compaction 等の疑い）と、次ブロック先読みが間に合う時間差を集計する。
引数: accesslog の pkl（an.py 出力）、debug の pkl（an2.py 出力）。"""
import pickle,sys
rd=pickle.load(open(sys.argv[1],'rb')); g=sorted(pickle.load(open(sys.argv[2],'rb'))['gets'])
slow=sorted((e['start'],e['end']) for e in rd if e['dur']>0.05)
no=[x for x in g if not any(a<x[1] and x[0]<b for a,b in slow)]
print('GETs not overlapping any slow QEMU read:',len(no),'of',len(g))
done={};gaps=[]
for s,e,k,_ in g:
    sid,idx,_=k.split('/')[-1].split('_'); idx=int(idx)
    if (sid,idx-1) in done: gaps.append(s-done[(sid,idx-1)][0])
    done[(sid,idx)]=(s,e)
gaps.sort(); n=len(gaps)
print('prev-block GET start -> this GET start: n',n,'p10 %.1fs p50 %.1fs p90 %.1fs'%(gaps[n//10],gaps[n//2],gaps[int(n*.9)]),'>=2s:',sum(x>=2 for x in gaps),'<2s:',sum(x<2 for x in gaps))
