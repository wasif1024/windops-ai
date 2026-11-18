// The frontend's dependency audit: npm audit at moderate and above, with a narrow exception list.
//
// An advisory may be excepted only while no fixed version exists, as the image scan's
// ignore-unfixed rule allows. Each exception in audit-exceptions.json names one advisory, its
// package and the last vulnerable version, a review date and the reason. The audit fails on:
//   - any advisory at moderate or above that is not excepted;
//   - an exception whose package has published a version past its last vulnerable one, as the
//     fix now exists and must be taken;
//   - an exception past its review date;
//   - an exception that no longer matches anything, so stale entries are removed.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const SEVERITIES = ["moderate", "high", "critical"];

function npm(...args) {
  try {
    return execFileSync("npm", args, { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] });
  } catch (error) {
    // npm audit exits non-zero when it finds anything; its report is still on stdout.
    if (error.stdout) return error.stdout;
    throw error;
  }
}

function newer(version, than) {
  const a = version.split(".").map(Number);
  const b = than.split(".").map(Number);
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0);
  }
  return false;
}

const exceptions = JSON.parse(readFileSync(new URL("../audit-exceptions.json", import.meta.url)));
const report = JSON.parse(npm("audit", "--json"));
const found = new Map();
for (const vulnerability of Object.values(report.vulnerabilities ?? {})) {
  for (const via of vulnerability.via) {
    if (typeof via === "object" && SEVERITIES.includes(via.severity)) {
      const id = via.url.split("/").pop();
      found.set(id, { package: via.name, severity: via.severity, title: via.title, url: via.url });
    }
  }
}

const failures = [];
const today = new Date().toISOString().slice(0, 10);
for (const [id, advisory] of found) {
  const exception = exceptions[id];
  if (!exception) {
    failures.push(`${advisory.severity}: ${advisory.package}, ${advisory.title} (${advisory.url})`);
  } else {
    console.log(`Excepted until ${exception.review_by}: ${id}, ${exception.package}`);
  }
}
for (const [id, exception] of Object.entries(exceptions)) {
  if (!found.has(id)) {
    failures.push(`${id} is excepted, but no longer reported: remove it from audit-exceptions.json`);
    continue;
  }
  if (today > exception.review_by) {
    failures.push(`${id}'s exception was to be reviewed by ${exception.review_by}`);
  }
  const latest = npm("view", exception.package, "version").trim();
  if (newer(latest, exception.vulnerable_through)) {
    failures.push(
      `${exception.package} ${latest} is published, after the last vulnerable ` +
        `${exception.vulnerable_through}: take it, and remove ${id} from audit-exceptions.json`,
    );
  }
}

if (failures.length) {
  console.error(`The dependency audit failed:\n- ${failures.join("\n- ")}`);
  process.exit(1);
}
console.log(`The dependency audit passed: ${found.size} advisories, all excepted for now`);
