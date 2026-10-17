"""Fixed observation pool and listing dates (not fund inception dates).

Listing dates were checked with authenticated Tushare fund_basic on 2026-10-17.
The latest date, 512100's 2016-11-04 listing, is also disclosed in the manager's
annual report / product summary: https://www.nffund.com/main/files/2026/03/30/798852809331.pdf
"""

ETF_UNIVERSE = [
    {"ts_code": "510300.SH", "name": "华泰柏瑞沪深300", "index": "沪深300", "list_date": "2012-05-28", "found_date": "2012-05-04"},
    {"ts_code": "510310.SH", "name": "易方达沪深300", "index": "沪深300", "list_date": "2013-03-25", "found_date": "2013-03-06"},
    {"ts_code": "510330.SH", "name": "华夏沪深300", "index": "沪深300", "list_date": "2013-01-16", "found_date": "2012-12-25"},
    {"ts_code": "159919.SZ", "name": "嘉实沪深300", "index": "沪深300", "list_date": "2012-05-28", "found_date": "2012-05-07"},
    {"ts_code": "510050.SH", "name": "华夏上证50", "index": "上证50", "list_date": "2005-02-23", "found_date": "2004-12-30"},
    {"ts_code": "510500.SH", "name": "南方中证500", "index": "中证500", "list_date": "2013-03-15", "found_date": "2013-02-06"},
    {"ts_code": "512100.SH", "name": "南方中证1000", "index": "中证1000", "list_date": "2016-11-04", "found_date": "2016-09-29"},
]
COMMON_LIST_DATE = max(item["list_date"] for item in ETF_UNIVERSE)
EARLIEST_DATA_DATE = min(item["found_date"] for item in ETF_UNIVERSE)
LISTING_DATES = {item["ts_code"]: item["list_date"] for item in ETF_UNIVERSE}
