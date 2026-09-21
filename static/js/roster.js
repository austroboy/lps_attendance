/*
 * The student roster: card numbers, face enrolment, and leaving the roll.
 *
 * Everything happens in place. The person using this is usually standing at a
 * terminal with a phone, working through a class one student at a time, so a
 * full page reload between each one would be miserable.
 */
(function () {
  const table = document.querySelector("[data-roster]");
  if (!table) return;

  const csrf = table.dataset.csrf;
  const statesUrl = table.dataset.statesUrl;
  const bar = document.getElementById("roster-status");

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrf,
        "X-Requested-With": "XMLHttpRequest",
      },
      body: new URLSearchParams(body),
    }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      return { ok: r.ok && data.ok !== false, data };
    });
  }

  function flash(message, kind) {
    if (!bar) return;
    bar.textContent = message;
    bar.className = "notice " + (kind || "success");
    bar.hidden = false;
    clearTimeout(bar._timer);
    bar._timer = setTimeout(() => { bar.hidden = true; }, kind === "error" ? 6000 : 3500);
  }

  /* ------------------------------------------------------------- redrawing */
  const FACE_HTML = {
    DONE: ['done', 'reset', '\u2713', 'Enrolled. Click to clear and do it again.'],
    PENDING: ['waiting', 'cancel', '\u22ef', 'Waiting at a terminal. Click to cancel.'],
    NONE: ['none', 'start', '\u2717', 'No face on file. Click to start enrolment.'],
  };

  function paint(row, state) {
    if (!row || !state) return;

    const device = row.querySelector('[data-cell="device"]');
    if (device) device.textContent = state.device_user_id || "\u2014";

    const faceCell = row.querySelector('[data-cell="face"]');
    if (faceCell) {
      const [cls, action, glyph, tip] = FACE_HTML[state.face] || FACE_HTML.NONE;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "face-btn " + cls + " js-face";
      button.dataset.action = action;
      button.textContent = glyph;
      button.title = state.face === "DONE" && state.face_detail
        ? "Enrolled — " + state.face_detail
          + (state.face_at ? " on " + state.face_at : "") + ". Click to clear and do it again."
        : tip;
      faceCell.replaceChildren(button);
    }

    const activeCell = row.querySelector('[data-cell="active"]');
    const actions = row.querySelector(".row-actions");
    if (activeCell && actions) {
      row.classList.toggle("is-off", !state.active);
      activeCell.replaceChildren(makeBadge(state.active));
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn ghost small " + (state.active ? "js-deactivate" : "js-reactivate");
      button.textContent = state.active ? "Remove" : "Restore";
      actions.replaceChildren(button);
    }

    const input = row.querySelector(".rfid-input");
    if (input && document.activeElement !== input) input.value = state.rfid || "";
  }

  function makeBadge(active) {
    const span = document.createElement("span");
    span.className = "badge " + (active ? "ok" : "bad");
    span.textContent = active ? "On roll" : "Off roll";
    return span;
  }

  /* ------------------------------------------------------------------ rfid */
  // Saves on blur, on Enter, and a beat after typing stops — a card reader
  // types the whole number in a burst and usually sends Enter, so that path
  // needs to work without anyone clicking anything.
  table.querySelectorAll(".rfid-input").forEach((input) => {
    let timer = null;
    let saved = input.value;

    const save = () => {
      clearTimeout(timer);
      const value = input.value.trim();
      if (value === saved) return;
      const row = input.closest("tr");
      input.classList.remove("saved", "failed");
      input.classList.add("saving");

      post(row.dataset.rfidUrl, { rfid: value }).then(({ ok, data }) => {
        input.classList.remove("saving");
        if (!ok) {
          input.classList.add("failed");
          input.value = saved;
          flash(data.error || data.message || "Could not save that card.", "error");
          return;
        }
        saved = data.rfid || "";
        input.value = saved;
        input.classList.add("saved");
        setTimeout(() => input.classList.remove("saved"), 1400);
        paint(row, data);
      }).catch(() => {
        input.classList.remove("saving");
        input.classList.add("failed");
        flash("Lost contact with the server.", "error");
      });
    };

    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(save, 800);
    });
    input.addEventListener("blur", save);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); save(); input.blur(); }
    });
  });

  /* ------------------------------------------------------------------ face */
  table.addEventListener("click", (event) => {
    const button = event.target.closest(".js-face");
    if (!button) return;
    const row = button.closest("tr");
    const action = button.dataset.action;

    if (action === "reset"
        && !confirm("Clear the face for " + row.dataset.name
                    + "? You will need to enrol it again.")) {
      return;
    }

    button.disabled = true;
    post(row.dataset.faceUrl, { action: action }).then(({ ok, data }) => {
      button.disabled = false;
      if (!ok) {
        flash(data.error || "That did not work.", "error");
        return;
      }
      paint(row, data);
      flash(data.message || "Done.");
      if (data.face === "PENDING") startPolling();
    }).catch(() => {
      button.disabled = false;
      flash("Lost contact with the server.", "error");
    });
  });

  /* ------------------------------------------------------- on and off roll */
  const modal = document.getElementById("leave-modal");
  let leavingRow = null;

  table.addEventListener("click", (event) => {
    if (event.target.closest(".js-deactivate")) {
      leavingRow = event.target.closest("tr");
      document.getElementById("leave-name").textContent = leavingRow.dataset.name;
      document.getElementById("leave-note").value = "";
      document.getElementById("leave-keep").checked = false;
      modal.hidden = false;
      document.body.classList.add("nav-open");
    }
    if (event.target.closest(".js-reactivate")) {
      const row = event.target.closest("tr");
      post(row.dataset.activeUrl, { active: "1" }).then(({ ok, data }) => {
        if (!ok) return flash(data.error || "Could not restore.", "error");
        paint(row, data);
        flash(data.message || "Restored.");
      });
    }
  });

  function closeModal() {
    modal.hidden = true;
    document.body.classList.remove("nav-open");
    leavingRow = null;
  }

  if (modal) {
    document.getElementById("leave-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.hidden) closeModal();
    });

    document.getElementById("leave-confirm").addEventListener("click", () => {
      if (!leavingRow) return;
      const row = leavingRow;
      post(row.dataset.activeUrl, {
        active: "0",
        reason: document.getElementById("leave-reason").value,
        note: document.getElementById("leave-note").value,
        keep_on_devices: document.getElementById("leave-keep").checked ? "1" : "0",
      }).then(({ ok, data }) => {
        closeModal();
        if (!ok) return flash(data.error || "Could not remove.", "error");
        paint(row, data);
        flash(data.message || "Removed from the roll.");
      }).catch(() => { closeModal(); flash("Lost contact with the server.", "error"); });
    });
  }

  /* --------------------------------------------------------------- polling */
  // Only runs while someone is actually waiting at a terminal, and gives up
  // after a few minutes so a forgotten tab does not poll all night.
  let timer = null;
  let ticks = 0;

  function waitingIds() {
    return Array.from(table.querySelectorAll("tr[data-student]"))
      .filter((row) => row.querySelector(".face-btn.waiting"))
      .map((row) => row.dataset.student);
  }

  function startPolling() {
    if (timer) return;
    ticks = 0;
    timer = setInterval(poll, 3000);
  }

  function stopPolling() {
    clearInterval(timer);
    timer = null;
  }

  function poll() {
    const ids = waitingIds();
    if (!ids.length) { stopPolling(); return; }
    if (++ticks > 120) {            // six minutes
      stopPolling();
      flash("Stopped checking for new enrolments. Reload to carry on.", "warning");
      return;
    }
    if (document.hidden) return;

    fetch(statesUrl + "?ids=" + ids.join(","),
          { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then((r) => r.json())
      .then((data) => {
        (data.students || []).forEach((state) => {
          const row = table.querySelector('tr[data-student="' + state.id + '"]');
          if (!row) return;
          const was = row.querySelector(".face-btn.waiting");
          paint(row, state);
          if (was && state.face === "DONE") {
            flash(row.dataset.name + " — face enrolled" +
                  (state.face_detail ? " (" + state.face_detail + ")" : "") + ".");
          }
        });
      })
      .catch(() => { /* a dropped poll is not worth shouting about */ });
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && waitingIds().length) startPolling();
  });

  if (waitingIds().length) startPolling();
})();
