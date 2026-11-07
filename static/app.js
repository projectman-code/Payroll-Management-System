(() => {
  const $ = (sel) => document.querySelector(sel);
  const money = (n) => "Rs. " + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const todayStr = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

  let employees = [];

  async function api(url, method = "GET", body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("Please log in.");
    }
    const data = await res.json().catch(() => ({ ok: false, error: "Server error." }));
    if (!res.ok || !data.ok) throw new Error(data.error || "Something went wrong.");
    return data;
  }

  let toastTimer;
  function toast(msg, isError = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (isError ? " err" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), 3200);
  }
  const fail = (e) => toast(e.message, true);

  // ---------- stats ----------
  async function loadStats() {
    const s = await api("/api/stats");
    $("#statPayroll").textContent = money(s.payroll_net);
    $("#statEmployees").textContent = s.employees;
    $("#statDepts").textContent = s.departments;
    $("#statAbsent").textContent = s.absent_today;
  }

  // ---------- employees ----------
  function renderEmployees() {
    const q = $("#search").value.trim().toLowerCase();
    const list = employees.filter((e) =>
      [e.name, e.emp_id, e.department].some((v) => v.toLowerCase().includes(q)));
    $("#employeesBody").innerHTML = list.map((e) => `<tr>
      <td class="mono">${esc(e.emp_id)}</td>
      <td>${esc(e.name)}</td>
      <td>${esc(e.department)}</td>
      <td>${esc(e.designation)}</td>
      <td class="num">${money(e.basic)}</td>
      <td class="num">${money(e.allowances)}</td>
      <td class="actions">
        <button class="mini" data-act="edit" data-id="${esc(e.emp_id)}">Edit</button>
        <button class="mini danger" data-act="delete" data-id="${esc(e.emp_id)}">Delete</button>
      </td></tr>`).join("");
    $("#emptyEmployees").hidden = list.length > 0;
  }

  async function loadEmployees() {
    employees = (await api("/api/employees")).employees;
    renderEmployees();
  }

  // ---------- modal ----------
  const modal = $("#modal");
  let onSubmit = null;
  function openModal({ title, fields, submitLabel = "Save", danger = false, submit }) {
    $("#modalTitle").textContent = title;
    $("#modalFields").innerHTML = fields;
    $("#modalError").hidden = true;
    const btn = $("#modalSubmit");
    btn.textContent = submitLabel;
    btn.className = "btn " + (danger ? "danger" : "primary");
    onSubmit = submit;
    modal.showModal();
    const first = $("#modalFields input");
    if (first) first.focus();
  }
  $("#modalCancel").addEventListener("click", () => modal.close());
  $("#modalForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = Object.fromEntries(new FormData(e.target));
    const btn = $("#modalSubmit");
    btn.disabled = true;
    try {
      await onSubmit(form);
      modal.close();
    } catch (err) {
      $("#modalError").textContent = err.message;
      $("#modalError").hidden = false;
    } finally {
      btn.disabled = false;
    }
  });

  const empFields = (e = {}) => `
    ${e.emp_id ? "" : `<label class="field">Employee ID <span class="hint">letters or digits, e.g. EMP001</span>
      <input name="emp_id" required maxlength="15"></label>`}
    <label class="field">Full name <input name="name" required maxlength="60" value="${esc(e.name || "")}"></label>
    <div class="two">
      <label class="field">Department <input name="department" required maxlength="40" value="${esc(e.department || "")}"></label>
      <label class="field">Designation <input name="designation" required maxlength="40" value="${esc(e.designation || "")}"></label>
    </div>
    <div class="two">
      <label class="field">Basic salary / month <input name="basic" type="number" min="1" step="0.01" required value="${e.basic ?? ""}"></label>
      <label class="field">Allowances / month <input name="allowances" type="number" min="0" step="0.01" value="${e.allowances ?? 0}"></label>
    </div>
    <label class="field">Joining date <input name="join_date" type="date" required value="${esc(e.join_date || todayStr())}"></label>`;

  $("#openAdd").addEventListener("click", () =>
    openModal({
      title: "New employee", submitLabel: "Add employee", fields: empFields(),
      submit: async (f) => { await api("/api/employees", "POST", f); toast("Employee added"); await refreshAll(); },
    }));

  $("#employeesBody").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const emp = employees.find((e) => e.emp_id === btn.dataset.id);
    if (!emp) return;
    if (btn.dataset.act === "edit") {
      openModal({
        title: `Edit ${emp.emp_id}`, fields: empFields(emp),
        submit: async (f) => { await api(`/api/employees/${encodeURIComponent(emp.emp_id)}`, "PUT", f); toast("Employee updated"); await refreshAll(); },
      });
    } else {
      openModal({
        title: `Delete ${emp.emp_id}?`, submitLabel: "Delete", danger: true,
        fields: `<p>This deletes <strong>${esc(emp.name)}</strong> and all attendance records. It cannot be undone.</p>`,
        submit: async () => { await api(`/api/employees/${encodeURIComponent(emp.emp_id)}`, "DELETE"); toast("Employee deleted"); await refreshAll(); },
      });
    }
  });
  $("#search").addEventListener("input", renderEmployees);

  // ---------- attendance ----------
  const STATUS_LABEL = { P: "Present", A: "Absent", H: "Half day", L: "Leave" };
  async function loadAttendance() {
    const day = $("#attDate").value;
    if (!day) return;
    const data = await api("/api/attendance?date=" + encodeURIComponent(day));
    $("#attBody").innerHTML = data.records.map((r) => {
      const st = r.status || "P";
      const opts = Object.entries(STATUS_LABEL)
        .map(([k, v]) => `<option value="${k}" ${k === st ? "selected" : ""}>${v}</option>`).join("");
      return `<tr><td class="mono">${esc(r.emp_id)}</td><td>${esc(r.name)}</td><td>${esc(r.department)}</td>
        <td><select class="status-select ${st}" data-id="${esc(r.emp_id)}">${opts}</select></td></tr>`;
    }).join("");
    $("#emptyAtt").hidden = data.records.length > 0;
  }
  $("#attBody").addEventListener("change", (e) => {
    if (e.target.matches(".status-select")) e.target.className = "status-select " + e.target.value;
  });
  $("#attDate").addEventListener("change", () => loadAttendance().catch(fail));
  $("#markAll").addEventListener("click", () =>
    document.querySelectorAll(".status-select").forEach((s) => { s.value = "P"; s.className = "status-select P"; }));
  $("#saveAtt").addEventListener("click", async () => {
    const records = [...document.querySelectorAll(".status-select")].map((s) => ({ emp_id: s.dataset.id, status: s.value }));
    if (!records.length) return toast("No employees to save.", true);
    try {
      const r = await api("/api/attendance", "POST", { date: $("#attDate").value, records });
      toast(`Attendance saved for ${r.saved} employee(s)`);
      await Promise.all([loadStats(), loadPayroll()]);
    } catch (e) { fail(e); }
  });

  // ---------- payroll ----------
  async function loadPayroll() {
    const month = $("#payMonth").value;
    if (!month) return;
    const d = await api("/api/payroll?month=" + encodeURIComponent(month));
    $("#totGross").textContent = money(d.total_gross);
    $("#totDed").textContent = money(d.total_deductions);
    $("#totNet").textContent = money(d.total_net);
    $("#payBody").innerHTML = d.rows.map((r) => `<tr>
      <td class="mono">${esc(r.emp_id)}</td><td>${esc(r.name)}</td>
      <td class="num">${r.present}</td><td class="num">${r.absent}</td>
      <td class="num">${money(r.gross)}</td><td class="num">${money(r.total_deductions)}</td>
      <td class="num"><strong>${money(r.net)}</strong></td>
      <td class="actions"><a class="mini" target="_blank" rel="noopener"
        href="/api/payslip/${encodeURIComponent(r.emp_id)}?month=${encodeURIComponent(month)}">PDF</a></td></tr>`).join("");
    $("#emptyPay").hidden = d.rows.length > 0;
  }
  $("#payMonth").addEventListener("change", () => loadPayroll().catch(fail));

  // ---------- tabs ----------
  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      ["employees", "attendance", "payroll"].forEach((n) => ($("#tab-" + n).hidden = n !== tab.dataset.tab));
      if (tab.dataset.tab === "attendance") loadAttendance().catch(fail);
      if (tab.dataset.tab === "payroll") loadPayroll().catch(fail);
    }));

  async function refreshAll() {
    await Promise.all([loadEmployees(), loadStats(), loadAttendance(), loadPayroll()]);
  }

  $("#attDate").value = todayStr();
  $("#payMonth").value = todayStr().slice(0, 7);
  refreshAll().catch(fail);
})();
