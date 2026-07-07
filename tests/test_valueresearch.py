"""Value Research parsers — pure, browserless tests against captured HTML."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))
import valueresearch as vr


RISK_HTML = '''<table class="table datatable-fixedheader" width="100%"><thead><tr>
<th class="col-hide-mob"><div></div></th>
<th class="text-right"><div>Mean Return (%) <img></div></th>
<th class="text-right"><div>Std Dev (%) <img></div></th>
<th class="text-right"><div>Sharpe (%) <img></div></th>
<th class="text-right"><div>Sortino (%) <img></div></th>
<th class="text-right"><div>Beta (%) <img></div></th>
<th class="text-right"><div>Alpha (%) <img></div></th>
<th class="text-right"><div>Information Ratio (%) <img></div></th></tr></thead>
<tbody>
<tr><td class="no-sort"><div>Quant Multi Cap Dir</div></td><td class="text-right"><div>14.28</div></td><td class="text-right"><div>18.37</div></td><td class="text-right"><div>0.46</div></td><td class="text-right"><div>0.76</div></td><td class="text-right"><div>1.04</div></td><td class="text-right"><div>-0.35</div></td><td class="text-right"><div><a><img></a></div></td></tr>
<tr><td class="no-sort"><div>VR Multicap TRI</div></td><td class="text-right"><div>14.26</div></td><td class="text-right"><div>16.52</div></td><td class="text-right"><div>0.51</div></td><td class="text-right"><div>0.71</div></td><td class="text-right"><div>--</div></td><td class="text-right"><div>--</div></td><td class="text-right"><div><a></a></div></td></tr>
<tr><td class="no-sort"><div>Equity: Multi Cap <img></div></td><td class="text-right"><div>17.59</div></td><td class="text-right"><div>16.40</div></td><td class="text-right"><div>0.72</div></td><td class="text-right"><div>0.94</div></td><td class="text-right"><div>0.96</div></td><td class="text-right"><div>3.67</div></td><td class="text-right"><div><a></a></div></td></tr>
<tr><td class="no-sort"><div>Rank within category</div></td><td class="text-right"><div>18</div></td><td class="text-right"><div>19</div></td><td class="text-right"><div>19</div></td><td class="text-right"><div>17</div></td><td class="text-right"><div>18</div></td><td class="text-right"><div>19</div></td><td></td></tr>
<tr><td class="no-sort"><div>Number of funds in category</div></td><td class="text-right"><div>19</div></td><td class="text-right"><div>19</div></td><td class="text-right"><div>19</div></td><td class="text-right"><div>19</div></td><td class="text-right"><div>19</div></td><td class="text-right"><div>19</div></td><td></td></tr>
</tbody></table>'''

MGR_HTML = '''<div class="vr-fund-manager-details test-fund-manager"><h2>Fund Manager</h2>
<div role="tablist"><p data-target="#fund-manager-2098"><img> Ayusha Kumbhat <span>since 19-Feb-2025</span></p><div id="fund-manager-2098" class="collapse show"><p><strong>Education:</strong> CFA Level III</p><p><strong>Experience:</strong> Research Analyst for the past 15 months.</p><p><strong>Funds Managed:</strong></p><ul><li><a class="managed-fund-name" href="/a">Quant BFSI Fund</a> - since Feb 2025</li><li><a class="managed-fund-name" href="/b">Quant Flexi Cap Fund</a> - since Feb 2025</li></ul></div></div>
<div role="tablist"><p data-target="#fund-manager-1674"><img> Sandeep Tandon <span>since 03-Feb-2025</span></p><div id="fund-manager-1674" class="collapse show"><p><strong>Experience:</strong> Founder/CIO of Quant MF; ICICI Securities VP.</p><p><strong>Funds Managed:</strong></p><ul><li><a class="managed-fund-name" href="/d">Quant Flexi Cap Fund</a> - since Jan 2022</li></ul></div></div>
<div role="tablist"><p data-target="#fund-manager-1677"><img> Ankit A Pande <span>since 11-May-2020</span></p><div id="fund-manager-1677" class="collapse show"><p><strong>Experience:</strong> CFA and MBA; equity research since 2011.</p><p><strong>Funds Managed:</strong></p><ul><li><a class="managed-fund-name" href="/g">Quant Flexi Cap Fund</a> - since May 2020</li><li><a class="managed-fund-name" href="/h">Quant Mid Cap Fund</a> - since May 2020</li></ul></div></div>
<div role="tablist"><p data-target="#fund-manager-1062"><img> Sanjeev Sharma <span>since 03-Oct-2019</span></p><div id="fund-manager-1062" class="collapse show"><p><strong>Experience:</strong> 17 years total, 13 in financial markets.</p><p><strong>Funds Managed:</strong></p><ul><li><a class="managed-fund-name" href="/j">Quant Flexi Cap Fund</a> - since Oct 2019</li></ul></div></div>
</div>'''


# ---- date parsing ---------------------------------------------------------
def test_parse_vro_date_formats():
    assert vr.parse_vro_date("19-Feb-2025") == "2025-02-19"
    assert vr.parse_vro_date("since 03-Oct-2019") == "2019-10-03"
    assert vr.parse_vro_date("Feb 2025") == "2025-02-01"
    assert vr.parse_vro_date("--") is None


# ---- risk table -----------------------------------------------------------
def test_risk_table_reads_sortino_by_row():
    risk = vr.parse_risk_table(RISK_HTML)
    assert risk["fund"]["sortino"] == 0.76
    assert risk["benchmark"]["sortino"] == 0.71
    assert risk["category"]["sortino"] == 0.94
    assert risk["fund"]["name"] == "Quant Multi Cap Dir"
    assert risk["benchmark"]["name"] == "VR Multicap TRI"


def test_risk_table_keeps_other_metrics_and_handles_dashes():
    risk = vr.parse_risk_table(RISK_HTML)
    assert risk["fund"]["sharpe"] == 0.46
    assert risk["category"]["alpha"] == 3.67
    assert risk["benchmark"]["beta"] is None       # "--" -> None


def test_risk_table_ignores_rank_and_count_rows():
    # only the first 3 body rows (fund/benchmark/category) become entries
    risk = vr.parse_risk_table(RISK_HTML)
    assert set(risk.keys()) == {"fund", "benchmark", "category"}


def test_risk_table_column_index_is_header_driven():
    # Sortino must be read from the header-matched column, not a fixed index
    reordered = RISK_HTML.replace("Sortino (%)", "ZZZ").replace("Beta (%)", "Sortino (%)")
    risk = vr.parse_risk_table(reordered)
    assert risk["fund"]["sortino"] == 1.04         # now the (old Beta) column


# ---- managers -------------------------------------------------------------
def test_parse_managers_all_and_ordered():
    mgrs = vr.parse_fund_managers(MGR_HTML)
    assert [m["name"] for m in mgrs] == [
        "Ayusha Kumbhat", "Sandeep Tandon", "Ankit A Pande", "Sanjeev Sharma"]
    assert mgrs[0]["since"] == "2025-02-19"
    assert "15 months" in mgrs[0]["experience"]
    assert mgrs[2]["funds_managed_count"] == 2


def test_summarize_managers_primary_vs_longest():
    summ = vr.summarize_managers(vr.parse_fund_managers(MGR_HTML), "2026-07-07")
    assert summ["team_size"] == 4
    assert summ["primary"]["name"] == "Ayusha Kumbhat"
    assert summ["primary"]["tenure_years"] == 1.38
    # the correction: a 6.76y veteran actually oversees the fund
    assert summ["longest"]["name"] == "Sanjeev Sharma"
    assert summ["longest"]["tenure_years"] == 6.76


def test_build_manual_entry_shape_matches_stage35():
    risk = vr.parse_risk_table(RISK_HTML)
    mgrs = vr.parse_fund_managers(MGR_HTML)
    e = vr.build_manual_entry("Quant Multi Cap Fund Growth Option Direct Plan",
                              "diversifier", risk, mgrs, "2026-07-07", "file://x")
    assert e["sortino"]["fund"] == 0.76 and e["sortino"]["category"] == 0.94
    # the gate keys on the longest-tenured hand, with team context preserved
    assert e["manager"]["name"] == "Sanjeev Sharma"
    assert e["manager"]["since"] == "2019-10-03"
    assert e["manager"]["team_size"] == 4
    assert e["manager"]["primary"]["name"] == "Ayusha Kumbhat"
