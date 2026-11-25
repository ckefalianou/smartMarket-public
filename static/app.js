const productsBody = document.querySelector("#products-body");
const emptyState = document.querySelector("#empty-state");
const pageStatus = document.querySelector("#page-status");
const collectButton = document.querySelector("#collect-button");

function euro(value) {
  return new Intl.NumberFormat("el-GR", {
    style: "currency",
    currency: "EUR",
  }).format(value);
}

function setStatus(message, kind = "") {
  pageStatus.textContent = message;
  pageStatus.className = `status ${kind}`.trim();
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `Σφάλμα αιτήματος (${response.status})`);
  }
  return data;
}

function addCell(row, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text;
  row.append(cell);
  return cell;
}

function renderProduct(product) {
  const row = document.createElement("tr");
  const nameCell = document.createElement("td");
  const link = document.createElement("a");
  link.href = product.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = product.name || "Αναμονή πρώτης συλλογής";
  nameCell.append(link);
  if (product.last_error) {
    const error = document.createElement("small");
    error.className = "product-error";
    error.textContent = product.last_error;
    nameCell.append(error);
  }
  row.append(nameCell);
  addCell(row, product.category || "—");

  addCell(
    row,
    product.current_price == null ? "—" : `${euro(product.current_price)} / τεμ.`,
  );
  addCell(
    row,
    product.average_price == null ? "—" : `${euro(product.average_price)} / τεμ.`,
  );

  const offer = product.current_offer
    ? `${product.current_offer} (κανονικά ${euro(product.current_list_price)} / τεμ.)`
    : "—";
  addCell(row, offer);
  const savings = product.current_offer
    ? `${euro(product.current_savings_per_item)} / τεμ. · ${euro(product.current_savings_per_bundle)} / προσφορά`
    : "—";
  addCell(row, savings);

  const comparisonCell = document.createElement("td");
  if (product.current_price == null) {
    comparisonCell.textContent = "Χωρίς ιστορικό";
  } else if (product.below_average) {
    comparisonCell.textContent = "Κάτω από μ.ό.";
    comparisonCell.className = "below-average";
  } else {
    comparisonCell.textContent = "—";
  }
  row.append(comparisonCell);

  addCell(row, product.best_weekday || "Χρειάζονται περισσότερες μέρες");
  const historyCell = addCell(row, "");
  historyCell.className = "history";
  const history = document.createElement("ul");
  for (const point of [...product.history].reverse()) {
    const item = document.createElement("li");
    item.textContent = point.offer
      ? `${point.date}: ${euro(point.price)} / τεμ. — ${point.offer}; κέρδος ${euro(point.savings_per_item)} / τεμ. (${euro(point.savings_per_bundle)} / προσφορά)`
      : `${point.date}: ${euro(point.price)} / τεμ.`;
    history.append(item);
  }
  historyCell.append(history);

  const actionsCell = document.createElement("td");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "button button-danger";
  remove.textContent = "Αφαίρεση";
  remove.addEventListener("click", async () => {
    if (!window.confirm(`Να αφαιρεθεί το «${product.name || product.url}» και το ιστορικό του;`)) {
      return;
    }
    try {
      await request(`/api/products/${encodeURIComponent(product.id)}`, { method: "DELETE" });
      await refresh();
      setStatus("Το προϊόν και το ιστορικό του αφαιρέθηκαν.", "success");
    } catch (error) {
      setStatus(error.message, "error");
    }
  });
  actionsCell.append(remove);
  row.append(actionsCell);
  return row;
}

async function refresh() {
  const [products, status] = await Promise.all([
    request("/api/products"),
    request("/api/status"),
  ]);
  productsBody.replaceChildren(...products.map(renderProduct));
  emptyState.hidden = products.length > 0;

  document.querySelector("#location-note").textContent = status.store_url
    ? `Κατάστημα: ${status.store_name} (${status.store_url})`
    : "Δεν έχει ρυθμιστεί κατάστημα. Όρισε PRICE_TRACKER_STORE_URL στο .env.";
  document.querySelector("#store-name").textContent = status.store_name;
  document.querySelector("#product-url").placeholder = status.store_url
    ? `${status.store_url}/...`
    : "https://...";
  document.querySelector("#telegram-note").textContent = status.telegram_configured
    ? "Το Telegram είναι ρυθμισμένο. Η χειροκίνητη συλλογή στέλνει επίσης αναφορά."
    : "Το Telegram δεν έχει ρυθμιστεί ακόμη. Πρόσθεσε TELEGRAM_BOT_TOKEN και TELEGRAM_CHAT_ID στο .env.";
  document.querySelector("#schedule-info").textContent =
    `Προγραμματισμένη συλλογή: καθημερινά στις ${status.daily_time} (${status.timezone})`;
  document.querySelector("#last-run-info").textContent = status.last_run_at
    ? `Τελευταία συλλογή: ${status.last_run_at}${status.last_run_error ? ` — ${status.last_run_error}` : ""}`
    : "Δεν έχει εκτελεστεί ακόμη συλλογή.";
}

document.querySelector("#product-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#product-url");
  try {
    await request("/api/products", {
      method: "POST",
      body: JSON.stringify({ url: input.value }),
    });
    input.value = "";
    await refresh();
    setStatus("Το προϊόν προστέθηκε. Κάνε συλλογή τώρα ή περίμενε την προγραμματισμένη ώρα.", "success");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

collectButton.addEventListener("click", async () => {
  collectButton.disabled = true;
  setStatus("Γίνεται συλλογή τιμών. Αυτό μπορεί να διαρκέσει λίγο.");
  try {
    const result = await request("/api/collect", {
      method: "POST",
      body: JSON.stringify({}),
    });
    await refresh();
    const telegram = result.telegram_sent
      ? " Η αναφορά στάλθηκε στο Telegram."
      : ` Η αποστολή Telegram απέτυχε: ${result.telegram_error || "άγνωστο σφάλμα"}.`;
    const failures = result.failed
      ? ` Αποτυχίες ανάγνωσης: ${result.failures.map((item) => `${item.name}: ${item.error}`).join("; ")}.`
      : "";
    setStatus(`Ολοκληρώθηκαν ${result.collected} τιμές.${telegram}${failures}`, result.failed ? "error" : "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    collectButton.disabled = false;
  }
});

refresh().catch((error) => setStatus(error.message, "error"));
