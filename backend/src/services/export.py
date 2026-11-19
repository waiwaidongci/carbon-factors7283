import csv,io,json
from fastapi import HTTPException
def csv_text(result):
 rows=result.get('rows',[])
 if not rows: raise HTTPException(409,'尚无结果')
 stream=io.StringIO();keys=list(dict.fromkeys(k for row in rows for k in row));w=csv.DictWriter(stream,fieldnames=keys);w.writeheader()
 for row in rows: w.writerow({k:(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v) for k,v in row.items()})
 return '﻿'+stream.getvalue()
def reconciliation_csv(result):
 if not result or not result.get('tie_out_ok'): raise HTTPException(409,'归因未通过完整对账，禁止导出')
 stream=io.StringIO()
 stream.write('对账结论,贡献加总,原报告差额,基准期总量,对比期总量\n')
 stream.write(','.join(['闭合' if result['tie_out_ok'] else '不闭合',result['components_total'],
                        result['report_delta'],result['original_base_total'],
                        result['original_current_total']])+'\n\n')
 w=csv.writer(stream)
 w.writerow(['部门','用量变动','因子变动','部门归属变动','部门合计'])
 for r in result['by_department']:
  w.writerow([r['department'],r['用量变动'],r['因子变动'],r['部门归属变动'],r['total']])
 return '﻿'+stream.getvalue()
