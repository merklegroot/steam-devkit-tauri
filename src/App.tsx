import { useCallback, useEffect, useMemo, useState } from "react";
import "./App.css";
import {
  connectByIp,
  forgetDevkit,
  getApiBaseUrl,
  getSelectedDevkit,
  listDevkits,
  listGames,
  pollTask,
  refreshStatus,
  registerDevkit,
  retryDevkit,
  runAction,
  selectDevkit,
  waitForBackend,
  type DevkitInfo,
} from "./api";

function stateLabel(state: DevkitInfo["state"]): string {
  switch (state) {
    case "devkit_init":
      return "Initializing…";
    case "devkit_registering":
      return "Registering…";
    case "devkit_init_failed":
      return "Init failed";
    case "devkit_not_registered":
      return "Needs registration";
    case "devkit_online":
      return "Online";
    default:
      return state;
  }
}

function App() {
  const [apiBase, setApiBase] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [devkits, setDevkits] = useState<DevkitInfo[]>([]);
  const [selected, setSelected] = useState<DevkitInfo | null>(null);
  const [ipAddress, setIpAddress] = useState("");
  const [ipPort, setIpPort] = useState("32000");
  const [busy, setBusy] = useState<string | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [games, setGames] = useState<unknown[] | null>(null);

  const pushLog = useCallback((msg: string) => {
    setLog((prev) => [`${new Date().toLocaleTimeString()} — ${msg}`, ...prev].slice(0, 80));
  }, []);

  const refresh = useCallback(async (base: string) => {
    const [kits, sel] = await Promise.all([listDevkits(base), getSelectedDevkit(base)]);
    setDevkits(kits);
    setSelected(sel);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const base = await getApiBaseUrl();
        if (cancelled) return;
        setApiBase(base);
        await waitForBackend(base);
        if (cancelled) return;
        await refresh(base);
        setReady(true);
        pushLog("Connected to devkit backend");
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    const timer = setInterval(() => {
      if (apiBase && ready) {
        refresh(apiBase).catch(() => {});
      }
    }, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [apiBase, ready, refresh, pushLog]);

  const onlineDevkits = useMemo(
    () => devkits.filter((d) => d.state === "devkit_online"),
    [devkits],
  );
  const pendingDevkits = useMemo(
    () => devkits.filter((d) => d.state !== "devkit_online"),
    [devkits],
  );

  const runWithTask = async (
    label: string,
    taskFn: () => Promise<string>,
  ) => {
    if (!apiBase) return;
    setBusy(label);
    try {
      const key = await taskFn();
      await pollTask(apiBase, key, (s) => {
        if (!s.done) setBusy(`${label}…`);
      });
      pushLog(`${label} completed`);
      await refresh(apiBase);
    } catch (e) {
      pushLog(`${label} failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const handleConnectIp = async () => {
    if (!apiBase || !ipAddress.trim()) return;
    setBusy("Connecting by IP");
    try {
      const port = ipPort.trim() ? parseInt(ipPort, 10) : undefined;
      await connectByIp(apiBase, ipAddress.trim(), port);
      pushLog(`Added devkit at ${ipAddress}`);
      await refresh(apiBase);
    } catch (e) {
      pushLog(`Connect failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const handleSelect = async (name: string) => {
    if (!apiBase) return;
    await selectDevkit(apiBase, name);
    await refresh(apiBase);
    setGames(null);
  };

  const handleRegister = async (name: string) => {
    if (!apiBase) return;
    await runWithTask(`Register ${name}`, () => registerDevkit(apiBase, name));
  };

  if (error) {
    return (
      <main className="app error-screen">
        <h1>Steam Devkit</h1>
        <p className="error">{error}</p>
        <pre className="hint">
          {`npm run setup:python\nnpm run tauri dev`}
        </pre>
      </main>
    );
  }

  if (!ready) {
    return (
      <main className="app loading-screen">
        <h1>Steam Devkit</h1>
        <p>Starting backend…</p>
      </main>
    );
  }

  return (
    <main className="app">
      <header className="header">
        <div>
          <h1>Steam Devkit</h1>
          <p className="subtitle">
            Tauri port of{" "}
            <a
              href="https://github.com/3Samourai/SteamOS-Devkit-Client-MacOS"
              target="_blank"
              rel="noreferrer"
            >
              SteamOS Devkit Client for macOS
            </a>
          </p>
        </div>
        {busy && <span className="badge busy">{busy}</span>}
      </header>

      <section className="panel connect-panel">
        <h2>Connect by IP</h2>
        <div className="row">
          <input
            placeholder="Steam Deck IP address"
            value={ipAddress}
            onChange={(e) => setIpAddress(e.target.value)}
          />
          <input
            className="port"
            placeholder="Port"
            value={ipPort}
            onChange={(e) => setIpPort(e.target.value)}
          />
          <button type="button" onClick={handleConnectIp} disabled={!!busy}>
            Connect
          </button>
        </div>
        <p className="hint">
          Enable Developer Mode on the device (Settings → System). Devices on the LAN
          are also discovered via mDNS automatically.
        </p>
      </section>

      <div className="columns">
        <section className="panel">
          <h2>Devkits</h2>
          {devkits.length === 0 && (
            <p className="muted">No devkits discovered yet.</p>
          )}

          {pendingDevkits.length > 0 && (
            <ul className="devkit-list pending">
              {pendingDevkits.map((kit) => (
                <li key={kit.name} className="devkit-card">
                  <div className="devkit-title">
                    <strong>{kit.full_name}</strong>
                    <span className="state">{stateLabel(kit.state)}</span>
                  </div>
                  {kit.limited_connectivity && (
                    <p className="warn">
                      Limited connectivity — check developer mode and ports 22 /{" "}
                      {kit.http_port}
                    </p>
                  )}
                  <div className="actions">
                    {kit.state === "devkit_not_registered" && (
                      <button
                        type="button"
                        onClick={() => handleRegister(kit.name)}
                        disabled={!!busy}
                      >
                        Register
                      </button>
                    )}
                    {kit.state === "devkit_init_failed" && (
                      <button
                        type="button"
                        onClick={async () => {
                          if (!apiBase) return;
                          await retryDevkit(apiBase, kit.name);
                          await refresh(apiBase);
                        }}
                        disabled={!!busy}
                      >
                        Retry
                      </button>
                    )}
                    {kit.added_by_ip && (
                      <button
                        type="button"
                        className="secondary"
                        onClick={async () => {
                          if (!apiBase) return;
                          await forgetDevkit(apiBase, kit.name);
                          await refresh(apiBase);
                        }}
                        disabled={!!busy}
                      >
                        Forget
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {onlineDevkits.length > 0 && (
            <>
              <h3 className="section-label">Online</h3>
              <ul className="devkit-list">
                {onlineDevkits.map((kit) => (
                  <li
                    key={kit.name}
                    className={
                      selected?.name === kit.name
                        ? "devkit-card selected"
                        : "devkit-card"
                    }
                  >
                    <label className="devkit-select">
                      <input
                        type="radio"
                        name="selected-devkit"
                        checked={selected?.name === kit.name}
                        onChange={() => handleSelect(kit.name)}
                      />
                      <div>
                        <strong>{kit.full_name}</strong>
                        {kit.is_steamos && kit.os_version && (
                          <span className="meta"> SteamOS {kit.os_version}</span>
                        )}
                        {!kit.user_password_is_set && (
                          <span className="warn-inline"> — password not set</span>
                        )}
                      </div>
                    </label>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>

        <section className="panel">
          <h2>Selected devkit</h2>
          {!selected && (
            <p className="muted">Select an online devkit to run actions.</p>
          )}
          {selected && (
            <>
              <dl className="status-grid">
                <dt>Steam client</dt>
                <dd>{selected.steam_client_status ?? "—"}</dd>
                <dt>Configuration</dt>
                <dd>{selected.steam_configuration ?? "—"}</dd>
                <dt>CEF debugging</dt>
                <dd>{selected.cef_debugging_enabled ? "enabled" : "disabled"}</dd>
                <dt>Login</dt>
                <dd>{selected.machine_login ?? "—"}</dd>
              </dl>
              <div className="actions toolbar">
                <button
                  type="button"
                  onClick={() =>
                    runWithTask("Refresh status", () =>
                      refreshStatus(apiBase!, selected.name),
                    )
                  }
                  disabled={!!busy}
                >
                  Refresh status
                </button>
                <button
                  type="button"
                  onClick={() =>
                    runWithTask("Remote shell", () =>
                      runAction(apiBase!, selected.name, "remote-shell"),
                    )
                  }
                  disabled={!!busy}
                >
                  Remote shell
                </button>
                <button
                  type="button"
                  onClick={() =>
                    runWithTask("Screenshot", () =>
                      runAction(apiBase!, selected.name, "screenshot", {}),
                    )
                  }
                  disabled={!!busy}
                >
                  Screenshot
                </button>
                <button
                  type="button"
                  onClick={() =>
                    runWithTask("Sync logs", () =>
                      runAction(apiBase!, selected.name, "sync-logs", {}),
                    )
                  }
                  disabled={!!busy}
                >
                  Sync logs
                </button>
                <button
                  type="button"
                  onClick={() =>
                    runWithTask("Restart session", () =>
                      runAction(apiBase!, selected.name, "restart-session"),
                    )
                  }
                  disabled={!!busy}
                >
                  Restart session
                </button>
                <button
                  type="button"
                  onClick={() =>
                    runWithTask("CEF console", () =>
                      runAction(apiBase!, selected.name, "cef-console"),
                    )
                  }
                  disabled={!!busy}
                >
                  CEF console
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={async () => {
                    if (!apiBase) return;
                    setBusy("Listing games");
                    try {
                      const g = await listGames(apiBase, selected.name);
                      setGames(g);
                      pushLog(`Found ${Array.isArray(g) ? g.length : 0} titles`);
                    } catch (e) {
                      pushLog(
                        `List games failed: ${e instanceof Error ? e.message : String(e)}`,
                      );
                    } finally {
                      setBusy(null);
                    }
                  }}
                  disabled={!!busy}
                >
                  List games
                </button>
              </div>
              {games && (
                <pre className="games-json">
                  {JSON.stringify(games, null, 2)}
                </pre>
              )}
            </>
          )}
        </section>

        <section className="panel log-panel">
          <h2>Activity</h2>
          <ul className="log">
            {log.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </section>
      </div>
    </main>
  );
}

export default App;
