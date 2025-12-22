let optionData = {};
const indexSelect = document.getElementById("indexSelect");
const expirySelect = document.getElementById("expirySelect");
const optionTable = document.getElementById("optionTable").querySelector("tbody");

fetch("public/option_chain.json")
  .then(res => res.json())
  .then(data => {
    optionData = data;
    loadIndices();
  });

function loadIndices() {
  Object.keys(optionData).forEach(index => {
    let opt = document.createElement("option");
    opt.value = index;
    opt.textContent = index;
    indexSelect.appendChild(opt);
  });
}

indexSelect.addEventListener("change", () => {
  expirySelect.innerHTML = '<option value="">Select Expiry</option>';
  optionTable.innerHTML = "";
  if (!indexSelect.value) return;

  const expiries = Object.keys(optionData[indexSelect.value]);
  expiries.forEach(exp => {
    let opt = document.createElement("option");
    opt.value = exp;
    opt.textContent = exp;
    expirySelect.appendChild(opt);
  });
});

expirySelect.addEventListener("change", () => {
  optionTable.innerHTML = "";
  if (!expirySelect.value) return;

  const index = indexSelect.value;
  const expiry = expirySelect.value;
  const strikes = optionData[index][expiry];

  Object.keys(strikes).sort((a, b) => parseInt(a) - parseInt(b)).forEach(strike => {
    const ce = strikes[strike]["CE"] || "";
    const pe = strikes[strike]["PE"] || "";

    const row = `<tr>
  <td class="strike">${strike}</td>
  <td class="ce">${ce}</td>
  <td class="pe">${pe}</td>
</tr>`;
    optionTable.innerHTML += row;
  });
});

function generateCommand() {
  const action = document.getElementById("actionSelect").value;
  const optionType = document.getElementById("optionTypeSelect").value;
  const strike = document.getElementById("strikeInput").value;
  const quantity = document.getElementById("quantityInput").value;
  const price = document.getElementById("priceInput").value;

  const index = document.getElementById("indexSelect").value;
  const expiry = document.getElementById("expirySelect").value;

  const expiryFormatted = expiry.replace(/-/g, '').toUpperCase(); // e.g., 2025-07-24 → 20250724 → JUL2025
  const monthMap = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const dateObj = new Date(expiry);
  const formattedExpiry = `${dateObj.getDate()}${monthMap[dateObj.getMonth()]}${dateObj.getFullYear()}`;

  let command = "";

  if (price === "" || parseFloat(price) === 0) {
    // LTP order format
    command = `${action.charAt(0).toUpperCase() + action.slice(1).toLowerCase()} ${quantity} ${index}${formattedExpiry}${strike}${optionType} at CP and Book at 1500`;
  } else {
    // Limit order format
    const sellPrice = (parseFloat(price) + 4).toFixed(1); // You can adjust the logic if needed
    command = `${action.charAt(0).toUpperCase() + action.slice(1).toLowerCase()} ${quantity} ${index}${formattedExpiry}${strike}${optionType} at ${price} and Sell at ${sellPrice}`;
  }

  document.getElementById("commandOutput").textContent = command;
}

async function fetchLTP(symbol) {
  try {
    const response = await fetch(`/ltp?symbol=${encodeURIComponent(symbol)}`);
    const data = await response.json();
    return data.ltp;
  } catch (error) {
    console.error(`❌ Failed to fetch LTP for ${symbol}`, error);
    return null;
  }
}

async function updateLTPs() {
  const rows = document.querySelectorAll("#optionTable tbody tr");
  for (const row of rows) {
    const ceCell = row.querySelector("td.ce");
    const peCell = row.querySelector("td.pe");

    const ceSymbol = ceCell.dataset.symbol;
    const peSymbol = peCell.dataset.symbol;

    const ceLtp = await fetchLTP(ceSymbol);
    const peLtp = await fetchLTP(peSymbol);

    if (ceLtp !== null) ceCell.innerText = ceLtp.toFixed(2);
    if (peLtp !== null) peCell.innerText = peLtp.toFixed(2);
  }
}

// Auto-refresh LTPs every 1 second
setInterval(updateLTPs, 1000);

