/*
 * Drag-and-drop assignment board, with a tap fallback.
 *
 * Works for both the timetable and the SMS schedules — the only difference is
 * the pair of endpoints passed in on the container's data attributes.
 *
 * Touch devices never fire HTML5 drag events, so on a phone the board was
 * simply inert: you could see the boxes and had no way to fill them. Tapping a
 * chip selects it, tapping a box drops it in. That path works with a mouse too,
 * which suits anyone who finds dragging fiddly.
 */
(function () {
  const board = document.querySelector("[data-board]");
  if (!board) return;

  const assignUrl = board.dataset.assignUrl;
  const assignClassUrl = board.dataset.assignClassUrl;
  const csrf = board.dataset.csrf;

  let picked = null;   // {kind, id, label} chosen by tapping

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrf,
        "X-Requested-With": "XMLHttpRequest",
      },
      body: new URLSearchParams(body),
    }).then((r) => r.json());
  }

  function flash(message, isError) {
    const bar = document.getElementById("board-status");
    if (!bar) return;
    bar.textContent = message;
    bar.className = "notice " + (isError ? "error" : "success");
    bar.hidden = false;
    clearTimeout(bar._timer);
    bar._timer = setTimeout(() => { bar.hidden = true; }, 2600);
  }

  /* ------------------------------------------------------------ selection */
  const hint = document.getElementById("board-hint");

  function setPicked(payload, chip) {
    document.querySelectorAll(".chip.picked").forEach((c) => c.classList.remove("picked"));
    picked = payload;
    if (payload && chip) {
      chip.classList.add("picked");
      if (hint) {
        hint.querySelector("[data-hint-label]").textContent = payload.label;
        hint.hidden = false;
      }
    } else if (hint) {
      hint.hidden = true;
    }
  }

  const cancel = document.getElementById("board-hint-cancel");
  if (cancel) cancel.addEventListener("click", () => setPicked(null));

  function payloadOf(chip) {
    return { kind: chip.dataset.kind, id: chip.dataset.id, label: chip.dataset.label };
  }

  /* ------------------------------------------------------------- dragging */
  document.querySelectorAll(".chip[draggable=true]").forEach((chip) => {
    chip.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/plain", JSON.stringify(payloadOf(chip)));
      event.dataTransfer.effectAllowed = "copy";
      chip.classList.add("dragging");
      setPicked(null);
    });
    chip.addEventListener("dragend", () => chip.classList.remove("dragging"));

    chip.addEventListener("click", () => {
      const already = picked && picked.kind === chip.dataset.kind
        && picked.id === chip.dataset.id;
      setPicked(already ? null : payloadOf(chip), chip);
    });
  });

  /* -------------------------------------------------------------- landing */
  function place(slot, payload) {
    const drop = slot.querySelector(".drop");

    if (payload.kind === "class") {
      post(assignClassUrl, { slot: slot.dataset.slot, target: payload.id })
        .then((data) => {
          if (!data.ok) return flash("That did not save. Reload and try again.", true);
          window.location.reload();
        })
        .catch(() => flash("Lost contact with the server.", true));
      return;
    }

    if (drop.querySelector('[data-target="' + payload.kind + ":" + payload.id + '"]')) {
      flash(payload.label + " is already in this box.");
      setPicked(null);
      return;
    }

    post(assignUrl, {
      slot: slot.dataset.slot, kind: payload.kind, target: payload.id, action: "add",
    })
      .then((data) => {
        if (!data.ok) return flash("That did not save. Reload and try again.", true);
        drop.appendChild(makeAssigned(payload, slot));
        flash(payload.label + " added to " + slot.dataset.name);
        setPicked(null);
      })
      .catch(() => flash("Lost contact with the server.", true));
  }

  document.querySelectorAll(".slot").forEach((slot) => {
    slot.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      slot.classList.add("over");
    });
    slot.addEventListener("dragleave", () => slot.classList.remove("over"));

    slot.addEventListener("drop", (event) => {
      event.preventDefault();
      slot.classList.remove("over");
      let payload;
      try {
        payload = JSON.parse(event.dataTransfer.getData("text/plain"));
      } catch (err) {
        return;
      }
      place(slot, payload);
    });

    // Tap path. A tap on the remove button or a link inside the box is not a
    // request to drop something into it.
    slot.addEventListener("click", (event) => {
      if (!picked) return;
      if (event.target.closest("button, a")) return;
      place(slot, picked);
    });
  });

  function makeAssigned(payload, slot) {
    const span = document.createElement("span");
    span.className = "assigned";
    span.dataset.target = payload.kind + ":" + payload.id;
    span.textContent = payload.label + " ";
    const button = document.createElement("button");
    button.type = "button";
    button.title = "Remove";
    button.setAttribute("aria-label", "Remove " + payload.label);
    button.textContent = "\u00d7";
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      remove(span, slot, payload.kind, payload.id);
    });
    span.appendChild(button);
    return span;
  }

  /* ------------------------------------------------------------- removing */
  function remove(node, slot, kind, id) {
    post(assignUrl, { slot: slot.dataset.slot, kind: kind, target: id, action: "remove" })
      .then((data) => {
        if (!data.ok) return flash("Could not remove that.", true);
        node.remove();
        flash("Removed from " + slot.dataset.name);
      })
      .catch(() => flash("Lost contact with the server.", true));
  }

  document.querySelectorAll(".assigned button").forEach((button) => {
    const node = button.closest(".assigned");
    const slot = button.closest(".slot");
    const [kind, id] = node.dataset.target.split(":");
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      remove(node, slot, kind, id);
    });
  });
})();
