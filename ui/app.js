let currentJobId = null;
let pollingTimer = null;

let isAuthenticated = false;

async function checkAuthStatus() {
  try {
    const resp = await fetch("/auth/status");
    if (!resp.ok) return;
    const data = await resp.json();

    const signinBtn = document.getElementById("google-signin-btn");
    const authStatus = document.getElementById("auth-status");
    const signoutBtn = document.getElementById("signout-btn");
    const userEmail = document.getElementById("user-email");
    const sheetCheckbox = document.getElementById("write-to-sheet-group");

    if (data.configured === false) {
      // OAuth not configured — hide sign-in button entirely
      if (signinBtn) signinBtn.style.display = "none";
      return;
    }

    if (data.authenticated) {
      isAuthenticated = true;
      if (signinBtn) signinBtn.style.display = "none";
      if (authStatus) {
        authStatus.style.display = "inline";
        if (userEmail) userEmail.textContent = data.email || "Authenticated";
      }
      if (signoutBtn) signoutBtn.style.display = "inline";
      if (sheetCheckbox) sheetCheckbox.style.display = "flex";
    } else {
      isAuthenticated = false;
      if (signinBtn) signinBtn.style.display = "inline-flex";
      if (authStatus) authStatus.style.display = "none";
      if (signoutBtn) signoutBtn.style.display = "none";
      if (sheetCheckbox) sheetCheckbox.style.display = "none";
    }
  } catch (e) {
    // ignore
  }
}

document.addEventListener("DOMContentLoaded", checkAuthStatus);

document.getElementById("google-signin-btn")?.addEventListener("click", () => {
  window.location.href = "/auth/google/login";
});

document.getElementById("signout-btn")?.addEventListener("click", async () => {
  try {
    await fetch("/auth/logout", { method: "POST" });
    await checkAuthStatus();
  } catch (e) {
    // ignore
  }
});

document.getElementById("email-form").addEventListener("submit", async (e) => {
  e.preventDefault();

  const formData = {
    sender_name: document.getElementById("sender_name").value.trim(),
    sender_role: document.getElementById("sender_role").value.trim(),
    sender_objective: document.getElementById("sender_objective").value.trim(),
    google_sheet_url: document.getElementById("sheet_url").value.trim(),
    start_row: parseInt(document.getElementById("start_row").value),
    end_row: parseInt(document.getElementById("end_row").value),
  };

  // Add write_to_sheet flag if authenticated and checkbox is checked
  const sheetCheckbox = document.getElementById("write-to-sheet-checkbox");
  if (isAuthenticated && sheetCheckbox && sheetCheckbox.checked) {
    formData.write_to_sheet = true;
  }

  // Validation
  if (formData.end_row < formData.start_row) {
    alert("End Row must be greater than or equal to Start Row.");
    return;
  }
  if (!formData.google_sheet_url.startsWith("https://docs.google.com/spreadsheets/d/")) {
    alert("Please enter a valid Google Sheets URL.");
    return;
  }

  // Switch to progress view
  document.getElementById("email-form").style.display = "none";
  document.getElementById("results-section").style.display = "none";
  document.getElementById("progress-section").style.display = "block";
  resetProgress();
  document.getElementById("stop-btn").disabled = false;
  document.getElementById("stop-btn").textContent = "Stop";

  try {
    const resp = await fetch("/mass_generate_email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formData),
    });

    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      throw new Error(errData.detail || `Server returned ${resp.status}`);
    }

    const data = await resp.json();
    currentJobId = data.job_id;
    startPolling(currentJobId);
  } catch (err) {
    showError(err.message);
  }
});

// Cancel button
document.getElementById("stop-btn").addEventListener("click", async () => {
  if (!currentJobId) return;
  document.getElementById("stop-btn").disabled = true;
  document.getElementById("stop-btn").textContent = "Stopping...";
  try {
    await fetch(`/mass_generate_email/cancel/${currentJobId}`, { method: "POST" });
  } catch (_) {
    // ignore
  }
});

function resetProgress() {
  document.getElementById("progress-bar").style.width = "0%";
  document.getElementById("progress-percent").textContent = "0%";
  document.getElementById("progress-status").textContent = "Starting...";
}

function startPolling(jobId) {
  const poll = async () => {
    try {
      const resp = await fetch(`/mass_generate_email/status/${jobId}`);
      if (!resp.ok) throw new Error(`Poll failed: ${resp.status}`);
      const data = await resp.json();

      const total = data.total || 1;
      const current = data.current || 0;
      const percent = Math.min(Math.round((current / total) * 100), 99);

      document.getElementById("progress-bar").style.width = percent + "%";
      document.getElementById("progress-percent").textContent = percent + "%";
      document.getElementById("progress-status").textContent =
        data.status === "running" || data.status === "pending"
          ? `Processing row ${current} of ${total}...`
          : data.status.charAt(0).toUpperCase() + data.status.slice(1);

      if (data.status === "running" || data.status === "pending") {
        pollingTimer = setTimeout(poll, 1500);
      } else if (data.status === "done" || data.status === "completed") {
        document.getElementById("progress-bar").style.width = "100%";
        document.getElementById("progress-percent").textContent = "100%";
        document.getElementById("progress-status").textContent = "Complete!";
        document.getElementById("stop-btn").disabled = true;
        showResults(data.results);
      } else if (data.status === "cancelled") {
        document.getElementById("stop-btn").disabled = true;
        document.getElementById("stop-btn").textContent = "Cancelled";
        document.getElementById("progress-status").textContent = "Job was cancelled.";
      } else if (data.status === "error") {
        document.getElementById("stop-btn").disabled = true;
        showError(data.error || "An error occurred during processing.");
      }
    } catch (err) {
      showError(err.message);
    }
  };
  pollingTimer = setTimeout(poll, 1500);
}

function showResults(results) {
  document.getElementById("progress-section").style.display = "none";
  document.getElementById("results-section").style.display = "block";

  const summary = document.getElementById("results-summary");
  const total = results?.total_requested || 0;
  const successful = results?.successful || 0;
  const skipped = results?.skipped || 0;
  const errors = total - successful - skipped;

  let summaryHtml = `✅ Completed: <strong>${successful}</strong>`;
  if (skipped > 0) summaryHtml += ` | ⏭️ Skipped: <strong>${skipped}</strong>`;
  if (errors > 0) summaryHtml += ` | ❌ Errors: <strong>${errors}</strong>`;
  summaryHtml += ` | Total: ${total}`;
  summary.innerHTML = summaryHtml;

  const table = document.getElementById("results-table");
  const items = results?.results || [];
  let html = `<thead><tr><th>#</th><th>Row</th><th>Subject</th><th>Status</th></tr></thead><tbody>`;
  items.forEach((item, i) => {
    const isSkipped = item.status === "skipped_no_context" || item.subject === "N/A";
    const statusClass = isSkipped ? "row-skipped" : "row-success";
    const statusText = isSkipped ? "⏭️ Skipped" : "✅ Done";
    const subject = isSkipped ? "—" : (item.subject || "");
    html += `<tr class="${statusClass}"><td>${i + 1}</td><td>${item.sheet_row ?? ""}</td><td>${escapeHtml(subject)}</td><td>${statusText}</td></tr>`;
  });
  html += "</tbody>";
  table.innerHTML = html;

  table.insertAdjacentHTML("afterend",
    '<p style="margin-top:16px"><a href="#" onclick="location.reload();return false">← Generate another batch</a></p>');
}

function showError(message) {
  document.getElementById("progress-section").style.display = "none";
  document.getElementById("results-section").style.display = "block";
  document.getElementById("results-summary").innerHTML =
    `<span class="error-msg">❌ Error: ${escapeHtml(message)}</span>` +
    '<p style="margin-top:12px"><a href="#" onclick="location.reload();return false">← Try again</a></p>';
}

function escapeHtml(text) {
  if (!text) return "";
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
