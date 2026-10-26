// 如月寮 · Galgame 分区的渲染（G-03）。用到 app.js 里的 el、safeLink、renderCover、conditionLabel、VERDICT_NAMES，
// 这些函数在调用时才查找，所以本文件可以先于 app.js 加载。
// 安全规则同 app.js：一律用 textContent，绝不用 innerHTML。

const GALGAME_EXAMPLES = [
  "2010 年以后的催泪作品，不要 NTR",
  "有没有百合的轮回题材",
  "全年龄的悬疑推理",
  "科幻、多结局",
];

const COVER_HIDDEN_TEXT = {
  not_safe: "非安全级封面（可在设置里打开）",
  minor_rule: "封面不显示",
};

function galgameConditionLabel(c) {
  const label = conditionLabel(c);
  return c.concept === "后宫" ? `${label}（指有全收结局）` : label;
}

function galEvidenceText(condition, ev) {
  const d = ev.detail || {};
  const concept = condition.concept;
  switch (ev.code) {
    case "year_in_range": return `${d.year} 年发售，在范围内`;
    case "year_out_of_range": return `${d.year} 年发售，不在范围内`;
    case "date_missing": return "没有发售日";
    case "vndb_tag_present": return `「${d.tag}」平均 ${d.score} 分（${d.votes} 票）`;
    case "vndb_tag_weak": return `「${d.tag}」平均 ${d.score} 分，只有 ${d.votes} 票，证据弱`;
    case "vndb_tag_denied": return `${d.negative} 票认为没有「${d.tag}」（平均 ${d.score} 分）`;
    case "vndb_tag_absent": return `没人给「${concept}」投票（这部作品共 ${d.tag_count} 个标签，证据不足）`;
    case "vndb_tag_absent_well_tagged": return `标签很齐全（${d.tag_count} 个），没有「${concept}」`;
    default: return ev.code;
  }
}

function renderGalEvidence(r) {
  const text = `${galgameConditionLabel(r.condition)}：${r.evidence.map((e) => galEvidenceText(r.condition, e)).join("；")}`;
  const li = el("li", {}, el("span", { class: `verdict v ${r.verdict}`, text: VERDICT_NAMES[r.verdict] }));
  const spoiler = r.evidence.some((e) => e.detail && e.detail.spoiler);
  if (!spoiler) {
    li.append(el("span", { text }));
    return li;
  }
  // 含剧透的标签：默认不显示标签名，点一下才显示
  const reveal = el("button", { type: "button", class: "toggle", text: `${galgameConditionLabel(r.condition)}：含剧透，点击显示依据` });
  reveal.addEventListener("click", () => reveal.replaceWith(el("span", { text })));
  li.append(reveal);
  return li;
}

function lengthText(minutes) {
  if (!minutes) return null;
  const hours = Math.round(minutes / 60);
  return hours >= 1 ? `约 ${hours} 小时` : `约 ${minutes} 分钟`;
}

function renderGalgameCard(item) {
  const title = item.title_zh || item.title_original;
  const year = item.release_year ? `${item.release_year}` : "发售日未定";
  const age = item.min_age != null ? (item.min_age >= 18 ? "18 禁" : item.min_age ? `${item.min_age}+` : "全年龄") : "分级未知";
  const score = item.rating != null ? `VNDB ${item.rating}（${item.vote_count} 票）` : "暂无评分";
  const meta = [year, age, score, lengthText(item.length_minutes)].filter(Boolean).join(" · ");

  const head = el("div", { class: "card-head" },
    el("div", {},
      el("h3", {}, safeLink(item.vndb_url, title)),
      item.title_zh && item.title_zh !== item.title_original
        ? el("div", { class: "original", text: item.title_original }) : null,
      el("div", { class: "meta", text: meta })),
    el("div", {},
      item.overall ? el("span", { class: `verdict ${item.overall}`, text: VERDICT_NAMES[item.overall] }) : null,
      item.weak_evidence ? el("span", { class: "weak", text: "证据薄弱" }) : null));

  const card = el("li", { class: "card" });
  const cover = renderCover(item.cover_url, title);
  if (cover) card.append(cover);
  else if (item.cover_hidden) card.append(el("div", { class: "cover cover-hidden", text: COVER_HIDDEN_TEXT[item.cover_hidden] || "封面不显示" }));
  else card.classList.add("no-cover");

  const body = el("div", { class: "card-body" }, head);
  card.append(body);
  if (item.has_all_ages && item.has_ero) body.append(el("span", { class: "badge pill", text: "有全年龄版" }));

  const evidence = el("ul", { class: "evidence" });
  for (const r of item.conditions) evidence.append(renderGalEvidence(r));
  body.append(evidence);

  if (item.summary) {
    const summary = el("p", { class: "summary", text: item.summary });
    const toggle = el("button", { type: "button", class: "toggle", text: "展开简介（多为英文）" });
    toggle.addEventListener("click", () => {
      const open = summary.classList.toggle("open");
      toggle.textContent = open ? "收起简介" : "展开简介（多为英文）";
    });
    body.append(summary, toggle);
  }

  const links = el("div", { class: "platforms" }, el("span", { class: "muted", text: "链接：" }), safeLink(item.vndb_url, "VNDB"));
  if (item.egs_url) links.append(safeLink(item.egs_url, "批評空間"));
  if (item.bangumi_url) links.append(safeLink(item.bangumi_url, "Bangumi"), el("span", { class: "muted", text: "（中文名来源）" }));
  body.append(links);
  // 改编动画（G-05）：点开是 Bangumi 条目页
  if (item.anime && item.anime.length) {
    body.append(renderLinks("改编动画：", item.anime.map((a) => ({
      url: a.bangumi_url, text: `《${a.title}》`, note: [a.year, FORMAT_NAMES[a.format]].filter(Boolean).join(" · "),
    }))));
  }
  return card;
}

function renderGalgameAgentItems(items) {
  $("results").hidden = !items.length;
  $("anime-notes").hidden = true;
  $("galgame-notes").hidden = false;
  $("total").textContent = `代理人推荐 ${items.length} 部`;
  $("counts").textContent = "（依据按代理人最后一次搜索的条件计算）";
  $("relax").hidden = true;
  document.querySelector(".pager").hidden = true;
  $("cards").replaceChildren(...items.map(renderGalgameCard));
}

function renderGalgameResults(data) {
  state.page = data.page;
  state.totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  $("results").hidden = false;
  $("anime-notes").hidden = true;
  $("galgame-notes").hidden = false;
  $("total").textContent = `共 ${data.total} 部`;
  const c = data.verdict_counts;
  $("counts").textContent =
    `（候选里：满足或很可能满足 ${c.likely_satisfied + c.satisfied}，未知 ${c.unknown}，冲突 ${c.conflict}）`;
  $("page-info").textContent = `第 ${data.page} / ${state.totalPages} 页`;
  document.querySelector(".pager").hidden = data.total === 0;
  updatePager();
  renderRelax(data.relax_hint, galgameConditionLabel);
  $("cards").replaceChildren(...data.items.map(renderGalgameCard));
}
