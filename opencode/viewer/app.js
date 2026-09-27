/* Loads the raw streams + gold windows from ../synth-data and renders the
   segmented view. The segmentation shown here is exactly what the Databricks
   gold stage emits; this viewer is a review tool, not the pipeline. */
const DATA = "../synth-data";

const state = { tasks: [], channels: [], issues: [], pulls: [], commits: [], sel: null };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const hhmm = (iso) => iso.slice(0, 10) + " " + iso.slice(11, 16);
const durMin = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 60000);

async function load() {
  const [gold, ref, channels, issues, pulls, commits] = await Promise.all([
    fetch(`${DATA}/gold/_index.json`).then((r) => r.json()),
    fetch(`${DATA}/gold/reference_windows.json`).then((r) => r.json()),
    fetchDiscord(),
    fetch(`${DATA}/raw/jira/issues.json`).then((r) => r.json()),
    fetch(`${DATA}/raw/github/pulls.json`).then((r) => r.json()),
    fetch(`${DATA}/raw/github/commits.json`).then((r) => r.json()),
  ]);
  const byJira = Object.fromEntries((ref?.tasks || []).map((t) => [t.jira, t]));
  state.tasks = (gold?.tasks || []).map((g) => ({ ...(byJira[g.jira] || {}), ...g }));
  state.issues = issues;
  state.pulls = pulls;
  state.commits = commits;
  renderList();
  if (state.tasks.length) select(state.tasks[0].task_id);
}

async function fetchDiscord() {
  // channels that exist in this dataset; a missing one must not 404-spam
  const names = ["billing", "eng-platform", "eng-frontend", "incidents", "deploys"];
  const out = [];
  for (const n of names) {
    const rec = await fetch(`${DATA}/raw/discord/${n}.json`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
    if (rec) out.push(...rec.messages.map((m) => ({ ...m, channel: rec.channel.name })));
  }
  return out;
}

function renderList() {
  const el = document.getElementById("tasklist");
  el.innerHTML = state.tasks.map((t) => `
    <div class="task" data-id="${t.task_id}">
      <span class="tid">${esc(t.task_id)} · ${esc(t.jira)} · PR #${t.pr}
        <span class="badge ${t.split}">${t.split}</span></span>
      <span class="ttl">${esc(t.title)}</span>
      <span class="when">${hhmm(t.window.start)} → ${hhmm(t.window.end)}</span>
    </div>`).join("");
  el.querySelectorAll(".task").forEach((n) =>
    n.addEventListener("click", () => select(n.dataset.id)));
}

function select(id) {
  state.sel = id;
  document.querySelectorAll(".task").forEach((n) =>
    n.classList.toggle("active", n.dataset.id === id));
  renderDetail(state.tasks.find((t) => t.task_id === id));
}

/** Parse a unified diff into per-file sections with stats. */
function parsePatch(text) {
  const files = [];
  let cur = null;
  for (const line of (text || "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/diff --git a\/(.+?) b\/(.+)$/);
      cur = { path: m ? m[2] : line, old: m ? m[1] : "", lines: [], add: 0, del: 0, newFile: false, deleted: false };
      files.push(cur);
      continue;
    }
    if (!cur) continue;
    if (line.startsWith("new file mode")) { cur.newFile = true; continue; }
    if (line.startsWith("deleted file mode")) { cur.deleted = true; continue; }
    if (line.startsWith("@@")) {
      cur.hunk = line;
      cur.lines.push({ t: "hunk", s: line });
      continue;
    }
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("index ") || line.startsWith("\\")) continue;
    if (line.startsWith("+")) { cur.add++; cur.lines.push({ t: "add", s: line }); continue; }
    if (line.startsWith("-")) { cur.del++; cur.lines.push({ t: "del", s: line }); continue; }
    if (line.startsWith(" ")) { cur.lines.push({ t: "ctx", s: line }); continue; }
  }
  return files;
}

function renderDiff(files, activePath) {
  if (!files.length) return '<p class="empty">no patch for this task</p>';
  const sel = files.find((f) => f.path === activePath) || files[0];
  const tabs = files.map((f) => {
    const on = f.path === sel.path;
    const tag = f.newFile ? "new" : f.deleted ? "del" : "";
    return `<button class="ftab ${on ? "on" : ""}" data-path="${esc(f.path)}">${esc(f.path.split("/").pop())}
      <span class="fstat ${f.add ? "a" : ""}${f.del ? " d" : ""}">+${f.add} −${f.del}</span>${tag ? `<span class="ftag-badge">${tag}</span>` : ""}</button>`;
  }).join("");
  const body = sel.lines.map((l) =>
    `<div class="dl ${l.t}">${esc(l.s)}</div>`).join("");
  return `<div class="diffwrap">
    <div class="ftabs">${tabs}</div>
    <div class="diffhead">${esc(sel.path)} <span class="fstat a">+${sel.add}</span><span class="fstat d">−${sel.del}</span></div>
    <div class="diff">${body}</div></div>`;
}

async function renderDetail(t) {
  const d = document.getElementById("detail");
  if (!t) { d.innerHTML = '<p class="empty">Pick a task.</p>'; return; }
  d.innerHTML = '<p class="empty">loading slice…</p>';

  // Read the GOLD slice: it is already carved by the model's timeframe and
  // already timestamp-normalized. Re-deriving the window from the raw bronze
  // stream here would fail on purpose-injected messy timestamps.
  const get = (f) => fetch(`${DATA}/gold/${t.task_id}/${f}`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const [sliceD, sliceJ, sliceG, brief, patch] = await Promise.all([
    get("discord.json"), get("jira.json"), get("git.json"), get("brief.json"),
    fetch(`${DATA}/gold/${t.task_id}/diff.patch`).then((r) => (r.ok ? r.text() : "")).catch(() => ""),
  ]);
  const diffFiles = parsePatch(patch);
  state.diffFiles = diffFiles;

  // ---- Discord segment (from the gold slice)
  const msgs = sliceD?.messages || [];
  const startId = sliceD?.messages?.[0]?.message_id;
  const endId = sliceD?.messages?.[msgs.length - 1]?.message_id;
  const discord = msgs.length ? msgs.map((m) => {
    const isStart = m.message_id === startId;
    const isEnd = m.message_id === endId;
    const att = (m.attachments || []).map((a) =>
      `<div class="att">📎 ${esc(a.filename)} (${a.bytes} B)</div>`).join("");
    return `<div class="msg ${isStart ? "boundary" : ""} ${isEnd ? "end" : ""}">
      <span class="ts">${hhmm(m.ts_utc)} · ${esc(m.channel || sliceD.channel)}</span>
      <div class="who">${esc(m.author_name)}${isStart ? " · START" : ""}${isEnd ? " · END" : ""}</div>
      <div class="txt">${esc(m.content)}</div>${att}</div>`;
  }).join("") : '<p class="empty">no messages in this slice</p>';

  // ---- Jira segment (from the gold slice)
  const iss = sliceJ?.issue;
  const evs = sliceJ?.changelog_events || [];
  const jira = iss ? `<div class="ev"><b>${esc(iss.issue_key)}</b> ${esc(iss.summary)}
      <div class="f">${esc(iss.status || "")} · ${esc(iss.assignee || "unassigned")}</div>
      <div class="f">created ${hhmm(iss.created_utc)} → updated ${hhmm(iss.updated_utc)}</div></div>` +
    evs.map((e) => `<div class="ev">
      <span class="f">${hhmm(e.ts_utc)} · ${esc(e.author)}</span><br>
      ${esc(e.field)}: <code>${esc(e.from_value || "∅")}</code> → <code>${esc(e.to_value || "∅")}</code>
    </div>`).join("")
    : '<p class="empty">no issue in this slice</p>';

  // ---- Git segment (from the gold slice)
  const cs = sliceG?.commits || [];
  const files = sliceG?.files || [];
  const pr = sliceG?.pull_request || {};
  const git = `
    <div class="ev"><b>PR #${t.pr ?? "?"}</b> ${esc(pr.title || "")}
      <div class="f">branch ${esc(pr.branch || "")} · ${esc(pr.author || "")} · opened ${hhmm(pr.opened_ts_utc || "")} · merged ${hhmm(pr.merged_ts_utc || "")}</div>
      <div class="f">base ${esc((sliceG?.base_sha || "").slice(0, 10))} → head ${esc((sliceG?.head_sha || "").slice(0, 10))}</div></div>
    <div class="ev"><b>${cs.length} commits</b> in this task · <b>${files.length} files</b> touched</div>
    ${files.map((f) => `<div class="f" style="padding-left:10px">${esc(f)}</div>`).join("")}
    <h3 style="margin-top:12px">commits</h3>
    ${cs.map((c) => `<div class="ev"><code>${esc(c.sha.slice(0, 8))}</code>
      <span class="f">${hhmm(c.authored_ts_utc)} · ${esc(c.author_name)}</span><br>${esc((c.message || "").split("\n")[0])}</div>`).join("")}`;

  // `document_files` (index) or `documents` (reference) holds filenames;
  // `documents` is a count on the index, so only accept a real array.
  const docList = Array.isArray(t.document_files) ? t.document_files
    : Array.isArray(t.documents) ? t.documents
    : Array.isArray(state.docsByTask?.[t.task_id]) ? state.docsByTask[t.task_id] : [];
  const docKind = (n) => (n.endsWith(".pdf") ? "jira/reports" : n.endsWith(".png") ? "discord/images" : "jira/logs");
  const docs = docList.map((n) =>
    `<div class="doc">📄 <a href="../synth-data/raw/attachments/${docKind(n)}/${n}" target="_blank">${esc(n)}</a></div>`).join("");

  d.innerHTML = `
    <div class="stat"><b>${esc(t.title)}</b> · ${esc(t.channel)} · ${esc(t.split)} split</div>
    <div class="winbar">
      <span class="pill start">${hhmm(t.window.start)}Z</span>
      <span class="pill">${durMin(t.window.start, t.window.end)} min</span>
      <span class="pill end">${hhmm(t.window.end)}Z</span>
    </div>
    <div class="tribal"><b>tribal constraint</b> — ${esc(t.tribal_constraint)}</div>
    <div class="grid3">
      <div class="col"><h3>discord · ${msgs.length} messages</h3>${discord}</div>
      <div class="col"><h3>jira changelog · ${evs.length} events</h3>${jira}${docs ? `<h3 style="margin-top:14px">documents · ${docList.length}</h3>${docs}` : ""}</div>
      <div class="col"><h3>github · ${cs.length} commits, ${files.length} files</h3>${git}</div>
    </div>
    <h3 class="difftitle">git diff · ${diffFiles.length} files · merge-base ${esc((sliceG?.base_sha || "").slice(0, 8))} → ${esc((sliceG?.head_sha || "").slice(0, 8))}</h3>
    ${renderDiff(diffFiles, null)}`;

  attachTabHandlers(d);
}

function attachTabHandlers(d) {
  d.querySelectorAll(".ftab").forEach((b) =>
    b.addEventListener("click", () => {
      d.querySelector(".diffwrap").outerHTML = renderDiff(state.diffFiles, b.dataset.path);
      d.querySelectorAll(".ftab").forEach((x) => x.classList.toggle("on", x.dataset.path === b.dataset.path));
      attachTabHandlers(d);
    }));
}

load();
