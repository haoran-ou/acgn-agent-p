// 如月寮：页面逻辑。
// 安全规则：数据库和用户输入里的文字一律用 textContent 写进页面，绝不用 innerHTML（防 XSS）。
// 分区：动画走 /chat（按分流规则交给固定流程或代理人）；Galgame 走 /galgame/ask（G-03）。

const ANIME_EXAMPLES = [
  "2010 年以后的 TV 动画，治愈系，不要后宫",
  "90 年代的机战，不要剧场版",
  "有没有主角之间互相救赎的百合番",
  "像 CLANNAD 那样的催泪番",
];

const SUBTITLES = {
  anime: "用一句话描述你想看的动画，每部结果都附上判断依据。",
  galgame: "用一句话描述你想玩的 galgame，标签按 VNDB 玩家投票判断。",
};

const FORMAT_NAMES = { tv: "TV", web: "WEB", ova: "OVA", movie: "剧场版", motion_comic: "动态漫画", other: "其他" };
const ALL_FORMATS = Object.keys(FORMAT_NAMES);
const VERDICT_NAMES = {
  satisfied: "满足", likely_satisfied: "很可能满足", unknown: "未知", conflict: "冲突",
};
const REGION_NAMES = {
  CN: "中国大陆", JP: "日本", TW: "台湾", HK: "香港", MO: "澳门", SG: "新加坡", MY: "马来西亚",
  KR: "韩国", US: "美国",
};
const FALLBACK_NAMES = {
  no_api_key: "未配置 AI，使用规则解析",
  forced: "已选择只用规则解析",
  timeout: "AI 超时，已改用规则解析",
  rate_limited: "AI 达到免费额度限制，已改用规则解析",
  invalid_output: "AI 输出格式不对，已改用规则解析",
};
const TOOL_NAMES = {
  find_works_by_title: "按名字找作品", search_works: "按条件搜索",
  get_work_details: "看详情", get_related_works: "看相关作品",
  find_vn_by_title: "按名字找作品", search_vn: "按条件搜索", get_vn_details: "看详情", get_related_vns: "看相关作品",
};
const ACCENTS = { "天青": "#3fd0ff", "樱": "#ff6fa8", "琥珀": "#ffb347", "翠": "#5ef2a0", "紫苑": "#a98bff" };
const TYPE_MS = 22; // 代理人的回答逐字显示：每个字的间隔
const AGENT_WAIT_MS = 1500; // 超过这个时间还没返回，多半是交给了代理人：提示要多等几秒

// 当前状态：一次解析之后，翻页、删条件、放宽、切换模式都直接调搜索接口，不再调用 AI
const state = {
  tab: "anime", conditions: [], understood: [], mode: "strict", page: 1, totalPages: 1, busy: false,
  history: [], // 和代理人的对话记录（服务器不保存，由页面发回去）
  settings: null,
  wasMissing: false, // 上一次检查时还没有数据：准备好时显示一句提示
};

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function safeLink(url, text) {
  // 只放行 http(s) 链接（服务器端也检查过）
  if (!url || !/^https?:\/\//.test(url)) return el("span", { text });
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text });
}

// ---------- 文字渲染 ----------

function conditionLabel(c) {
  switch (c.kind) {
    case "year":
      if (c.min_year != null && c.max_year != null) {
        return c.min_year === c.max_year ? `${c.min_year} 年` : `${c.min_year}–${c.max_year} 年`;
      }
      return c.min_year != null ? `${c.min_year} 年以后` : `${c.max_year} 年及以前`;
    case "format": {
      const wanted = c.formats;
      if (wanted.length >= ALL_FORMATS.length - 2) {
        const missing = ALL_FORMATS.filter((f) => !wanted.includes(f));
        return `不要${missing.map((f) => FORMAT_NAMES[f]).join("、")}`;
      }
      return wanted.map((f) => FORMAT_NAMES[f] || f).join(" / ");
    }
    case "tag_required":
      return `想要：${c.concept}`;
    case "tag_excluded":
      return `不要：${c.concept}`;
    default:
      return c.kind;
  }
}

function evidenceText(condition, ev) {
  const d = ev.detail || {};
  const concept = condition.concept;
  switch (ev.code) {
    case "year_in_range": return `${d.year} 年，在范围内`;
    case "year_out_of_range": return `${d.year} 年，不在范围内`;
    case "date_missing": return "没有日期信息";
    case "format_match": return `类型：${FORMAT_NAMES[d.format] || d.format}`;
    case "format_mismatch": return `类型是${FORMAT_NAMES[d.format] || d.format}`;
    case "format_missing": return "没有类型信息";
    case "meta_tag_present": return `公共标签里有「${d.tag}」`;
    case "user_tag_present": return `「${d.tag}」有 ${d.count} 人标记`;
    case "user_tag_weak": return `「${d.tag}」只有 ${d.count} 人标记`;
    case "tag_absent": return `标签里没有「${concept}」`;
    case "tag_absent_strong_upper_bound":
      return `标签很充分，但没有「${concept}」（没列出的标签最多 ${d.upper_bound} 人标记）`;
    case "tag_absent_low_upper_bound":
      return `没有「${concept}」，但证据不足（没列出的标签最多 ${d.upper_bound} 人标记）`;
    case "tag_absent_few_tags":
      return `没有「${concept}」，但这部作品只有 ${d.user_tag_count} 个标签，证据不足`;
    default: return ev.code;
  }
}

function regionText(regions) {
  if (!regions) return "地区未声明";
  return regions.map((r) => REGION_NAMES[r] || r).join("、");
}

function parserText(parse) {
  if (parse.parser === "llm") return `AI 解析 · ${parse.latency_ms} 毫秒`;
  const reason = parse.fallback_reason || "";
  const text = FALLBACK_NAMES[reason]
    || (reason.startsWith("http_") ? `AI 服务出错（${reason.slice(5)}），已改用规则解析` : "AI 出错，已改用规则解析");
  return `${text} · ${parse.latency_ms} 毫秒`;
}

// ---------- 请求 ----------

async function request(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = `请求失败（${resp.status}）`;
    try {
      const data = await resp.json();
      if (typeof data.detail === "string") detail = data.detail;
    } catch { /* 不是 JSON：用默认提示 */ }
    throw new Error(detail);
  }
  return resp.json();
}

async function post(path, body) {
  $("loading").textContent = "正在搜索…";
  $("loading").hidden = false;
  const timer = state.settings?.agent_enabled
    ? setTimeout(() => { $("loading").textContent = "代理人正在查询，大约需要 5–10 秒…"; }, AGENT_WAIT_MS)
    : null;
  try {
    return await request(path, body);
  } finally {
    clearTimeout(timer);
    $("loading").hidden = true;
  }
}

function galgameFlags() {
  return { adult_confirmed: !!state.settings?.adult_enabled, show_all_covers: !!state.settings?.show_all_covers };
}

async function ask(text) {
  setBusy(true);
  try {
    if (state.tab === "galgame") {
      const data = await post("/galgame/chat", { text, history: state.history, ...galgameFlags() });
      handleChat(data, renderGalgameResults, renderGalgameAgentItems);
    } else {
      const data = await post("/chat", { text, history: state.history });
      handleChat(data, renderResults, renderAgentItems);
    }
    if (state.history.length) $("ask-text").value = "";  // 在和代理人对话：清空，方便接着说
  } catch (err) {
    showMessage(err.message);
  } finally {
    setBusy(false);
  }
}

function showParsed(parse, clarification, search, render, routeNote) {
  state.understood = parse.understood;
  state.conditions = state.understood.map((u) => u.condition);
  $("edited").hidden = true;
  renderParse(parse, routeNote);
  if (clarification) {
    showMessage(clarification);
    $("results").hidden = true;
  } else {
    $("message").hidden = true;
    render(search);
  }
}

// 两个分区共用：render 渲染固定流程的结果，renderAgent 渲染代理人推荐的卡片
function handleChat(data, render, renderAgent) {
  if (data.workflow) {
    // 分给了固定流程，或者代理人没完成（退回固定流程）
    state.history = [];
    if (data.agent && !data.agent.used) {
      showAgent(data, `代理人这次没能完成（${data.agent.fallback_reason}），下面是按条件直接搜索的结果。`);
    } else {
      hideAgent();
    }
    const note = data.agent ? null : `固定流程：${data.route.reason}`;
    showParsed(data.workflow.parse, data.workflow.clarification, data.workflow.search, render, note);
    return;
  }
  // 代理人完成了
  state.history = data.history;
  state.conditions = [];
  $("understood").hidden = true;
  $("message").hidden = true;
  showAgent(data, data.reply);
  renderAgent(data.items);
}

function showAgent(data, reply) {
  $("agent").hidden = false;
  const info = data.agent;
  $("agent-badge").textContent = info && info.used
    ? `${(info.latency_ms / 1000).toFixed(1)} 秒 · ${info.usage.requests || 0} 次模型调用`
    : "已退回固定流程";
  typeText($("agent-reply"), reply);
  const question = $("agent-question");
  question.hidden = !data.question;
  question.textContent = data.question ? `代理人想确认：${data.question}（直接在上面的输入框回答）` : "";
  const tools = (info?.trace || []).map((t) => TOOL_NAMES[t.tool] || t.tool);
  $("agent-trace").textContent = `${data.route.reason}${tools.length ? ` · 查了：${tools.join(" → ")}` : ""}`;
  $("ask-text").placeholder = state.history.length ? "接着和代理人说…（或点“新对话”）" : "向代理人发出你们的需求，第二世代。";
}

let typing = null;

function typeText(node, text) {
  // galgame 式的逐字显示；点一下立刻显示全部。系统设置了“减少动态效果”时直接显示
  clearInterval(typing);
  node.classList.remove("done");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || !text) {
    node.textContent = text;
    node.classList.add("done");
    return;
  }
  let shown = 0;
  node.textContent = "";
  node.classList.add("typing");
  const finish = () => {
    clearInterval(typing);
    node.textContent = text;
    node.classList.remove("typing");
    node.classList.add("done");
    node.removeEventListener("click", finish);
  };
  node.addEventListener("click", finish);
  typing = setInterval(() => {
    shown += 1;
    node.textContent = text.slice(0, shown);
    if (shown >= text.length) finish();
  }, TYPE_MS);
}

function hideAgent() {
  $("agent").hidden = true;
}

function newChat() {
  state.history = [];
  hideAgent();
  $("ask-text").placeholder = "向代理人发出你们的需求，第二世代。";
  $("ask-text").focus();
}

function renderAgentItems(items) {
  $("results").hidden = !items.length;
  $("anime-notes").hidden = false;
  $("galgame-notes").hidden = true;
  $("total").textContent = `代理人推荐 ${items.length} 部`;
  $("counts").textContent = "（依据按代理人最后一次搜索的条件计算）";
  $("relax").hidden = true;
  document.querySelector(".pager").hidden = true;
  $("platform-notice").textContent = "放送平台数据来源：bangumi-data（CC BY 4.0）。曾在该平台上线的记录，未核实当前是否可看。";
  $("cards").replaceChildren(...items.map(renderCard));
}

async function searchAgain(page = 1) {
  if (!state.conditions.length) {
    showMessage("条件都删掉了。请重新输入一句话。");
    $("results").hidden = true;
    return;
  }
  setBusy(true);
  try {
    if (state.tab === "galgame") {
      const data = await post("/galgame/search", { conditions: state.conditions, mode: state.mode, page, ...galgameFlags() });
      $("message").hidden = true;
      renderGalgameResults(data);
    } else {
      const data = await post("/search", { conditions: state.conditions, mode: state.mode, page });
      $("message").hidden = true;
      renderResults(data);
    }
  } catch (err) {
    showMessage(err.message);
  } finally {
    setBusy(false);
  }
}

function setBusy(busy) {
  state.busy = busy;
  $("ask-button").disabled = busy;
  updatePager();
}

function updatePager() {
  // 翻页按钮的状态只由页码和是否忙碌决定；之前 setBusy(false) 会把它们统一设回可用（bug）
  $("prev").disabled = state.busy || state.page <= 1;
  $("next").disabled = state.busy || state.page >= state.totalPages;
}

function showMessage(text) {
  const box = $("message");
  box.textContent = text;
  box.hidden = false;
}

// ---------- 渲染 ----------

function renderParse(parse, routeNote) {
  $("understood").hidden = false;
  $("parser-badge").textContent = parserText(parse);
  renderChips();

  const route = $("route-note");
  route.hidden = !routeNote;
  route.textContent = routeNote || "";

  const overrides = $("overrides");
  overrides.hidden = !parse.rule_overrides.length;
  const names = { year: "年份", format: "类型" };
  overrides.textContent = `「${parse.rule_overrides.map((k) => names[k] || k).join("、")}」按规则识别的结果为准。`;

  const unmatched = $("unmatched");
  unmatched.hidden = !parse.unmatched.length;
  unmatched.textContent = `没理解的部分：${parse.unmatched.join("、")}`;
}

function renderChips() {
  const box = $("chips");
  box.replaceChildren();
  if (!state.understood.length) {
    box.append(el("span", { class: "muted", text: "没有识别出条件" }));
    return;
  }
  const label = state.tab === "galgame" ? galgameConditionLabel : conditionLabel;
  state.understood.forEach((u, index) => {
    const remove = el("button", { type: "button", title: "删掉这个条件", "aria-label": "删掉这个条件", text: "×" });
    remove.addEventListener("click", () => removeCondition(index));
    box.append(el("span", { class: "chip" },
      el("span", { text: label(u.condition) }),
      u.inferred ? el("span", { class: "tag", text: "推断", title: `由 AI 根据“${u.source_text}”推断` }) : null,
      remove));
  });
}

function removeCondition(index) {
  state.understood.splice(index, 1);
  state.conditions = state.understood.map((u) => u.condition);
  $("edited").hidden = false;
  renderChips();
  searchAgain(1);
}

function renderResults(data) {
  state.page = data.page;
  state.totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  $("results").hidden = false;
  $("anime-notes").hidden = false;
  $("galgame-notes").hidden = true;
  $("total").textContent = data.total === data.works_total
    ? `共 ${data.total} 部`
    : `共 ${data.total} 个系列（${data.works_total} 部作品）`;
  const c = data.verdict_counts;
  $("counts").textContent =
    `（候选里：满足或很可能满足 ${c.likely_satisfied + c.satisfied}，未知 ${c.unknown}，冲突 ${c.conflict}）`;
  $("page-info").textContent = `第 ${data.page} / ${state.totalPages} 页`;
  document.querySelector(".pager").hidden = data.total === 0;
  updatePager();
  $("platform-notice").textContent =
    `放送平台数据来源：${data.platform_notice.source}（${data.platform_notice.license}）。${data.platform_notice.note}。`;

  renderRelax(data.relax_hint, conditionLabel);
  const cards = $("cards");
  cards.replaceChildren(...data.items.map(renderCard));
}

function renderRelax(hint, label) {
  const box = $("relax");
  box.replaceChildren();
  box.hidden = !hint;
  if (!hint) return;
  box.append(el("p", { text: "严格模式下没有找到。可以放宽一个条件（系统不会自动放宽）：" }));
  for (const option of hint.drop_one) {
    const button = el("button", {
      type: "button", text: `去掉「${label(option.condition)}」→ ${option.strict_total_without} 部`,
    });
    button.addEventListener("click", () => removeCondition(option.condition_index));
    box.append(button);
  }
  if (hint.explore_total) {
    const button = el("button", { type: "button", text: `改用探索模式 → ${hint.explore_total} 部（多数是“未知”）` });
    button.addEventListener("click", () => setMode("explore", true));
    box.append(button);
  }
}

function renderCard(item) {
  const title = item.title_zh || item.title_original;
  const year = item.release_date ? item.release_date.slice(0, 4) : "年份未知";
  const score = item.score != null ? `评分 ${item.score}（${item.rating_count} 人）` : "暂无评分";
  const heading = item.bangumi_url ? safeLink(item.bangumi_url, title) : el("span", { text: title });

  const head = el("div", { class: "card-head" },
    el("div", {},
      el("h3", {}, heading),
      item.title_zh && item.title_zh !== item.title_original
        ? el("div", { class: "original", text: item.title_original }) : null,
      el("div", { class: "meta", text: `${year} · ${FORMAT_NAMES[item.format] || "类型未知"} · ${score}` })),
    el("div", {},
      // 代理人推荐、但没有搜索条件可对照时，没有整体判断
      item.overall ? el("span", { class: `verdict ${item.overall}`, text: VERDICT_NAMES[item.overall] }) : null,
      item.weak_evidence ? el("span", { class: "weak", text: "证据薄弱" }) : null));

  const evidence = el("ul", { class: "evidence" });
  for (const r of item.conditions) {
    evidence.append(el("li", {},
      el("span", { class: `verdict v ${r.verdict}`, text: VERDICT_NAMES[r.verdict] }),
      el("span", { text: `${conditionLabel(r.condition)}：${r.evidence.map((e) => evidenceText(r.condition, e)).join("；")}` })));
  }

  // 卡片 = 左边封面 + 右边正文（body）
  const card = el("li", { class: "card" });
  const cover = renderCover(item.cover_url, title);
  if (cover) card.append(cover);
  else card.classList.add("no-cover");
  const body = el("div", { class: "card-body" }, head);
  card.append(body);
  const series = item.series;
  if (series && series.entry_title) {
    body.append(el("p", { class: "note small", text: `这是续作，系列从《${series.entry_title}》开始。` }));
  }
  body.append(evidence);

  if (item.summary) {
    const summary = el("p", { class: "summary", text: item.summary });
    const toggle = el("button", { type: "button", class: "toggle", text: "展开简介" });
    toggle.addEventListener("click", () => {
      const open = summary.classList.toggle("open");
      toggle.textContent = open ? "收起简介" : "展开简介";
    });
    body.append(summary, toggle);
  }

  if (series && series.others_total) {
    const list = el("ul", { class: "series-list", hidden: "" });
    for (const o of series.others) {
      const year = o.release_date ? o.release_date.slice(0, 4) : "年份未知";
      list.append(el("li", {},
        el("span", { text: `${o.title} · ${year} · ${FORMAT_NAMES[o.format] || "类型未知"} ` }),
        el("span", { class: `verdict v ${o.overall}`, text: VERDICT_NAMES[o.overall] })));
    }
    if (series.others_total > series.others.length) {
      list.append(el("li", { class: "muted", text: `……还有 ${series.others_total - series.others.length} 部` }));
    }
    const label = `同系列还有 ${series.others_total} 部符合条件`;
    const toggle = el("button", { type: "button", class: "toggle", text: `${label} ▾` });
    toggle.addEventListener("click", () => {
      list.hidden = !list.hidden;
      toggle.textContent = `${label} ${list.hidden ? "▾" : "▴"}`;
    });
    body.append(el("div", { class: "series" }, toggle, list));
  }

  const platforms = el("div", { class: "platforms" });
  if (item.listed_platforms.length) {
    platforms.append(el("span", { class: "muted", text: "曾在以下平台上线：" }));
    for (const p of item.listed_platforms) {
      platforms.append(safeLink(p.url, p.name), el("span", { class: "region", text: `（${regionText(p.regions)}）` }));
    }
  } else {
    platforms.append(el("span", { class: "muted", text: "没有正版平台记录。可以在 Bangumi 页面查看。" }));
  }
  body.append(platforms);
  // 相关游戏（G-05，通常就是原作）：服务器只在成人开关打开时才给
  if (item.original_games && item.original_games.length) {
    body.append(renderLinks("相关游戏：", item.original_games.map((g) => ({
      url: g.vndb_url, text: `《${g.title}》`, note: [g.year, g.min_age >= 18 ? "18 禁" : null].filter(Boolean).join(" · "),
    }))));
  }
  return card;
}

function renderLinks(label, links) {
  const box = el("div", { class: "platforms links" }, el("span", { class: "muted", text: label }));
  for (const l of links.slice(0, 5)) {
    box.append(safeLink(l.url, l.text));
    if (l.note) box.append(el("span", { class: "region", text: `（${l.note}）` }));
  }
  if (links.length > 5) box.append(el("span", { class: "muted", text: `等 ${links.length} 部` }));
  return box;
}

function renderCover(url, title) {
  // 只放行 https 图片地址；滑到屏幕里才加载（loading=lazy）；加载失败就隐藏，卡片照常显示
  if (!url || !/^https:\/\//.test(url)) return null;
  const img = el("img", {
    class: "cover", src: url, alt: `《${title}》封面`, loading: "lazy", decoding: "async",
    referrerpolicy: "no-referrer", width: "80", height: "112",
  });
  img.addEventListener("error", () => {
    const card = img.closest(".card");  // 先找卡片，移除图片后就找不到了
    img.remove();
    if (card) card.classList.add("no-cover");
  });
  return img;
}

// ---------- 分区 ----------

function renderExamples() {
  const box = $("examples");
  box.replaceChildren();
  for (const text of state.tab === "galgame" ? GALGAME_EXAMPLES : ANIME_EXAMPLES) {
    const chip = el("button", { type: "button", class: "example", text });
    chip.addEventListener("click", () => {
      $("ask-text").value = text;
      ask(text);
    });
    box.append(chip);
  }
}

function setTab(tab) {
  if (tab === "galgame" && !state.settings?.adult_enabled) tab = "anime";
  state.tab = tab;
  for (const button of document.querySelectorAll(".tab")) {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }
  $("subtitle").textContent = SUBTITLES[tab];
  state.conditions = [];
  state.understood = [];
  state.history = [];
  for (const id of ["agent", "understood", "message", "results"]) $(id).hidden = true;
  $("ask-text").placeholder = "向代理人发出你们的需求，第二世代。";
  renderExamples();
}

// ---------- 数据更新（S-01）----------

function packageDate(pkg) {
  const m = (pkg || "").match(/(\d{4}-\d{2}-\d{2})/);
  return m ? m[1] : null;
}

function ageText(label, info) {
  if (!info || !info.imported_at) return `${label}：还没有（在设置里点“立即更新”，或运行 uv run acg-setup）`;
  const when = info.days === 0 ? "今天" : ` ${info.days} 天前`;
  const pkg = packageDate(info.package);
  return `${label}：本机更新于${when}${pkg ? `（数据包 ${pkg}）` : ""}`;
}

function renderOnboarding(data) {
  // 还没有动画数据：只显示“准备数据”，搜索框和分区先藏起来（搜了也只会报错）
  const missing = !data.anime.imported_at;
  $("onboarding").hidden = !missing;
  $("ask-form").hidden = missing;
  document.querySelector(".tabs").hidden = missing;
  $("data-age").hidden = missing;
  const job = data.job;
  $("setup-start").disabled = job.running;
  $("setup-start").textContent = job.running ? "正在准备…" : "开始准备数据";
  const bar = $("setup-bar-box");
  bar.hidden = !job.running;
  $("setup-bar").style.width = job.total ? `${Math.min(100, (job.done / job.total) * 100)}%` : "0";
  const progress = $("setup-progress");
  if (job.running) {
    progress.textContent = job.total
      ? `${job.stage}  ${Math.round(job.done / 1e6)} / ${Math.round(job.total / 1e6)} MB` : job.stage;
    progress.hidden = false;
  } else if (missing && job.results.some((r) => r.status === "failed")) {
    const failed = job.results.find((r) => r.status === "failed");
    progress.textContent = `没有完成：${failed.name}（${failed.detail}）。检查网络后再点一次，会接着下载。`;
    progress.hidden = false;
  }
  if (!missing && state.wasMissing) $("setup-done").hidden = false;  // 刚刚准备好
  state.wasMissing = missing;
}

function renderDataStatus(data) {
  renderOnboarding(data);
  const text = [ageText("动画数据", data.anime)];
  if (state.settings?.adult_enabled) text.push(ageText("Galgame 数据", data.galgame));
  $("data-age").textContent = text.join(" · ");
  $("data-status").textContent = [ageText("动画数据", data.anime), ageText("Galgame 数据", data.galgame)].join("；");
  const job = data.job;
  const progress = $("update-progress");
  $("update-now").disabled = job.running;
  if (job.running) {
    const amount = job.total ? `${Math.round(job.done / 1e6)} / ${Math.round(job.total / 1e6)} MB`
      : job.done ? `${Math.round(job.done / 1e6)} MB` : "";
    progress.textContent = `正在更新：${job.stage}${amount ? `  ${amount}` : ""}`;
    progress.hidden = false;
  } else if (job.results.length) {
    const names = { updated: "已更新", current: "已是最新", failed: "失败" };
    progress.textContent = "上次更新：" + job.results.map((r) => `${r.name} ${names[r.status] || r.status}${r.status === "failed" ? `（${r.detail}）` : ""}`).join("；");
    progress.hidden = false;
  }
}

let updatePoll = null;

async function loadDataStatus() {
  try {
    const data = await request("/data/status");
    renderDataStatus(data);
    if (data.job.running && !updatePoll) updatePoll = setInterval(loadDataStatus, 1000);
    if (!data.job.running && updatePoll) {
      clearInterval(updatePoll);
      updatePoll = null;
    }
  } catch {
    $("data-age").textContent = "";
  }
}

async function updateNow() {
  try {
    await request("/data/update", {});
    loadDataStatus();
  } catch (err) {
    $("update-progress").textContent = err.message;
    $("update-progress").hidden = false;
  }
}

// ---------- 外观（U-01）----------

function applyAppearance(a) {
  if (!a) return;
  const root = document.documentElement;
  root.dataset.theme = a.theme;
  root.style.setProperty("--accent", a.accent);
  root.style.setProperty("--bg-blur", `${a.bg_blur}px`);
  root.style.setProperty("--bg-dim", String(a.bg_dim / 100));
  root.style.setProperty("--panel-alpha", String(a.panel_opacity / 100));
  root.style.setProperty("--font-scale", String(a.font_scale / 100));
  root.style.setProperty("--bg-image", a.background ? `url("/appearance/background?v=${a.background_version}")` : "none");
}

function draftAppearance() {
  // 设置页上现在的值（还没保存）
  return {
    ...(state.settings?.appearance || {}),
    theme: $("set-theme").value,
    accent: $("set-accent").value,
    bg_blur: Number($("set-blur").value),
    bg_dim: Number($("set-dim").value),
    panel_opacity: Number($("set-panel").value),
    font_scale: Number($("set-font").value),
  };
}

function renderSwatches(accent) {
  for (const b of document.querySelectorAll(".swatch")) b.classList.toggle("active", b.dataset.color === accent);
}

function fillAppearance(a) {
  $("set-theme").value = a.theme;
  $("set-accent").value = a.accent;
  $("set-blur").value = a.bg_blur;
  $("set-dim").value = a.bg_dim;
  $("set-panel").value = a.panel_opacity;
  $("set-font").value = a.font_scale;
  $("remove-bg").disabled = !a.background;
  renderSwatches(a.accent);
}

function previewAppearance() {
  const a = draftAppearance();
  applyAppearance(a);
  renderSwatches(a.accent);
}

async function uploadBackground() {
  const file = $("set-bg").files[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    showSettingsError("图片太大：最大 10 MB");
    return;
  }
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("读不了这张图片"));
    reader.readAsDataURL(file);
  });
  try {
    const keep = draftAppearance(); // 上传会刷新设置：先记下还没保存的调整
    state.settings = await request("/appearance/background", { data_url: dataUrl });
    applyAppearance({ ...keep, background: state.settings.appearance.background,
                      background_version: state.settings.appearance.background_version });
    $("remove-bg").disabled = false;
  } catch (err) {
    showSettingsError(err.message);
  } finally {
    $("set-bg").value = "";
  }
}

async function removeBackground() {
  try {
    const keep = draftAppearance();
    const resp = await fetch("/appearance/background", { method: "DELETE" });
    if (!resp.ok) throw new Error(`请求失败（${resp.status}）`);
    state.settings = await resp.json();
    applyAppearance({ ...keep, background: null });
    $("remove-bg").disabled = true;
  } catch (err) {
    showSettingsError(err.message);
  }
}

function showSettingsError(text) {
  const box = $("settings-error");
  box.textContent = text;
  box.hidden = false;
}

// ---------- 设置 ----------

async function loadSettings() {
  try {
    state.settings = await request("/settings");
  } catch {
    state.settings = null;
  }
  applyAppearance(state.settings?.appearance);
  renderModelStatus();
  $("tab-galgame").hidden = !state.settings?.adult_enabled;
  if (state.tab === "galgame" && !state.settings?.adult_enabled) setTab("anime");
}

function renderModelStatus() {
  // 主页面和设置页顶部都写明现在用的是什么：哪家 · 哪个型号，或者只用规则
  const s = state.settings;
  if (!s) return;
  const name = providerOf(s.provider).name || s.provider;
  let parts;
  if (s.using_ai) parts = ["正在使用：", el("b", { text: `${name} · ${s.model}` })];
  else if (s.provider === "none") parts = ["正在使用：", el("b", { text: "规则解析（没有 AI）" }), "：免费、最快，理解不了复杂的说法，代理人不可用"];
  else parts = ["正在使用：", el("b", { text: "规则解析（没有 AI）" }), `：${name} 还没有填 Key`];
  for (const id of ["model-status", "settings-model-status"]) $(id).replaceChildren(...parts.map((x) => (typeof x === "string" ? document.createTextNode(x) : x.cloneNode(true))));
}

function providerOf(id) {
  return (state.settings?.providers || []).find((p) => p.id === id) || {};
}

function renderProvider() {
  // 选了哪家服务商：显示它的说明、候选型号、申请 Key 的链接，以及这一家的 Key 状态
  const s = state.settings || {};
  const id = $("set-provider").value;
  const p = providerOf(id);
  const note = $("provider-note");
  note.replaceChildren(el("span", { text: p.note || "" }));
  if (p.key_url) note.append(el("span", { text: " · " }), safeLink(p.key_url, "去申请 Key"));
  $("provider-fields").hidden = id === "none";  // 不用 AI：没有型号、Key 可填
  renderModels(p);
  $("base-url-row").hidden = id !== "custom";
  $("set-base-url").value = s.base_url || "";
  $("key-label").textContent = `${p.name || ""} 的 API Key`;
  $("set-key").value = "";
  $("set-clear-key").checked = false;
  $("set-clear-key").disabled = !p.has_key;
  $("key-status").textContent = p.has_key
    ? `已有这一家的 Key（${p.key_hint}）`
    : "还没有这一家的 Key：填了才会用 AI；不填就只用规则解析。";
}

const CUSTOM_MODEL = "__custom__";

function renderModels(p) {
  // 型号用下拉菜单：候选型号（第一个是默认的）+ 之前手填过的 + “自己填写…”。
  // 不用 <input list>：框里有字时浏览器只显示匹配的候选，手填过别的型号后就切不回默认的了
  const models = p.models || [];
  const options = models.map((m, i) => el("option", { value: m, text: i === 0 ? `${m}（默认）` : m }));
  if (p.model && !models.includes(p.model)) options.push(el("option", { value: p.model, text: `${p.model}（你填的）` }));
  options.push(el("option", { value: CUSTOM_MODEL, text: "自己填写…" }));
  $("set-model").replaceChildren(...options);
  $("set-model").value = p.model || CUSTOM_MODEL;
  $("set-model-custom").value = "";
  $("model-custom-row").hidden = $("set-model").value !== CUSTOM_MODEL;
}

function chosenModel() {
  return $("set-model").value === CUSTOM_MODEL ? $("set-model-custom").value.trim() : $("set-model").value;
}

function renderFreeKeys() {
  // 有免费额度的几家放在一起（说明里写了“免费”的），每家一个申请链接
  const list = $("free-key-list");
  list.replaceChildren(...(state.settings?.providers || [])
    .filter((p) => p.key_url && p.note.includes("免费"))
    .map((p) => el("li", {}, safeLink(p.key_url, p.name), el("span", { text: `：${p.note}` }))));
}

function openSettings() {
  const s = state.settings || {};
  renderFreeKeys();
  if (s.appearance) fillAppearance(s.appearance);
  $("set-provider").replaceChildren(...(s.providers || []).map((p) => el("option", { value: p.id, text: p.name })));
  $("set-provider").value = s.provider || "";
  renderProvider();
  $("test-result").hidden = true;
  $("set-agent").checked = !!s.agent_enabled;
  $("set-agent-always").checked = !!s.agent_always;
  $("set-adult").checked = !!s.adult_enabled;
  $("set-age").checked = false;
  $("age-row").hidden = true;
  $("set-covers").checked = !!s.show_all_covers;
  $("set-covers").disabled = !s.adult_enabled;
  $("settings-error").hidden = true;
  $("unsaved-box").hidden = true;
  renderModelStatus();
  $("settings-dialog").showModal();
}

function settingsBody() {
  const s = state.settings || {};
  const body = {};
  const provider = $("set-provider").value;
  if (provider && provider !== s.provider) body.provider = provider;
  const key = $("set-key").value.trim();
  if (key) body.api_key = key;
  else if ($("set-clear-key").checked) body.api_key = "";
  const model = chosenModel();
  if (model && (model !== s.model || body.provider)) body.model = model;
  if (provider === "custom" && $("set-base-url").value.trim() !== (s.base_url || "")) body.base_url = $("set-base-url").value.trim();
  return body;
}

async function testConnection() {
  // 先保存服务商、Key、型号，再用一句很短的话试一下
  const box = $("test-result");
  box.hidden = false;
  box.textContent = "正在测试…";
  try {
    const body = settingsBody();
    if (Object.keys(body).length) state.settings = await request("/settings", body);
    renderProvider();
    renderModelStatus();
    const r = await request("/settings/test", {});
    box.textContent = r.ok ? `✓ ${r.detail}` : `✗ ${r.detail}`;
  } catch (err) {
    box.textContent = `✗ ${err.message}`;
  }
}

function pendingChanges() {
  // 设置页里改了、还没保存的东西（背景图是选了就上传的，不算在这里）
  const s = state.settings || {};
  const body = settingsBody();
  if ($("set-agent").checked !== s.agent_enabled) body.agent_enabled = $("set-agent").checked;
  if ($("set-agent-always").checked !== s.agent_always) body.agent_always = $("set-agent-always").checked;
  if ($("set-adult").checked !== s.adult_enabled) {
    body.adult_enabled = $("set-adult").checked;
    body.confirm_age = $("set-age").checked;
  }
  if ($("set-covers").checked !== s.show_all_covers) body.show_all_covers = $("set-covers").checked;
  const draft = draftAppearance();
  const changed = Object.fromEntries(Object.entries(draft).filter(([k, v]) =>
    !["background", "background_version"].includes(k) && v !== s.appearance?.[k]));
  if (Object.keys(changed).length) body.appearance = changed;
  return body;
}

async function saveSettings(event) {
  event?.preventDefault();
  const body = pendingChanges();
  try {
    if (Object.keys(body).length) state.settings = await request("/settings", body);
    $("tab-galgame").hidden = !state.settings.adult_enabled;
    if (state.tab === "galgame" && !state.settings.adult_enabled) setTab("anime");
    renderModelStatus();
    $("settings-dialog").close();
    loadDataStatus();
  } catch (err) {
    $("unsaved-box").hidden = true;
    const box = $("settings-error");
    box.textContent = err.message;
    box.hidden = false;
  }
}

function requestCloseSettings() {
  // 右上角的 × 和 Esc：有没保存的更改时先问一句（返回修改 / 不保存，关闭 / 保存并关闭）
  if (!Object.keys(pendingChanges()).length) {
    $("settings-dialog").close();
    return;
  }
  $("unsaved-box").hidden = false;
  $("unsaved-save").focus();
}

function onAdultToggle() {
  const turningOn = $("set-adult").checked && !state.settings?.adult_enabled;
  $("age-row").hidden = !turningOn;
  $("set-covers").disabled = !$("set-adult").checked;
  if (!$("set-adult").checked) $("set-covers").checked = false;
}

// ---------- 事件 ----------

function setMode(mode, rerun) {
  state.mode = mode;
  document.querySelector(`input[name="mode"][value="${mode}"]`).checked = true;
  $("mode-hint").textContent = mode === "strict"
    ? "严格：只显示有证据支持的作品"
    : "探索：也显示证据不足（未知）的作品";
  if (rerun && state.conditions.length) searchAgain(1);
}

$("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("ask-text").value.trim();
  if (text) ask(text);
});
for (const radio of document.querySelectorAll('input[name="mode"]')) {
  radio.addEventListener("change", () => setMode(radio.value, true));
}
for (const button of document.querySelectorAll(".tab")) {
  button.addEventListener("click", () => setTab(button.dataset.tab));
}
$("prev").addEventListener("click", () => searchAgain(state.page - 1));
$("next").addEventListener("click", () => searchAgain(state.page + 1));
$("new-chat").addEventListener("click", newChat);
$("open-settings").addEventListener("click", openSettings);
$("settings-cancel").addEventListener("click", () => $("settings-dialog").close());
// 关掉对话框（取消、Esc）时，没保存的外观调整恢复原样
$("settings-dialog").addEventListener("close", () => applyAppearance(state.settings?.appearance));
for (const id of ["set-theme", "set-accent", "set-blur", "set-dim", "set-panel", "set-font"]) {
  $(id).addEventListener("input", previewAppearance);
}
for (const [name, color] of Object.entries(ACCENTS)) {
  const b = el("button", { type: "button", class: "swatch", title: name, "aria-label": `主题色：${name}`,
                           style: `background:${color};color:${color}` });
  b.dataset.color = color;
  b.addEventListener("click", () => { $("set-accent").value = color; previewAppearance(); });
  $("swatches").insertBefore(b, $("set-accent"));
}
$("set-bg").addEventListener("change", uploadBackground);
$("remove-bg").addEventListener("click", removeBackground);
$("settings-form").addEventListener("submit", saveSettings);
$("settings-close").addEventListener("click", requestCloseSettings);
$("settings-dialog").addEventListener("cancel", (event) => {  // 按 Esc：和点 × 一样，先检查有没有没保存的
  event.preventDefault();
  requestCloseSettings();
});
$("unsaved-back").addEventListener("click", () => { $("unsaved-box").hidden = true; });
$("unsaved-discard").addEventListener("click", () => $("settings-dialog").close());
$("unsaved-save").addEventListener("click", () => saveSettings());
$("set-adult").addEventListener("change", onAdultToggle);
$("update-now").addEventListener("click", updateNow);
$("set-provider").addEventListener("change", renderProvider);
$("set-model").addEventListener("change", () => {
  $("model-custom-row").hidden = $("set-model").value !== CUSTOM_MODEL;
  if (!$("model-custom-row").hidden) $("set-model-custom").focus();
});
$("test-connection").addEventListener("click", testConnection);
$("setup-start").addEventListener("click", updateNow);

renderExamples();

// 地址里带 ?q=一句话 时自动搜索（方便分享搜索链接）；?mode=explore 可以指定模式；?tab=galgame 打开 Galgame 分区
const params = new URLSearchParams(location.search);
if (params.get("mode") === "explore") setMode("explore", false);
loadSettings().then(() => {
  loadDataStatus();
  if (params.get("tab") === "galgame") setTab("galgame");
  if (params.get("q")) {
    $("ask-text").value = params.get("q").slice(0, 200);
    ask($("ask-text").value);
  }
});
