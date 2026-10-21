"use client";
import { useState } from "react";
import { api, setToken } from "@/lib/api";
import { TopBar } from "@/lib/ui";

export default function Login() {
  const [token, setT] = useState("");
  const [err, setErr] = useState("");

  async function signIn(e: React.FormEvent) {
    e.preventDefault();
    setToken(token.trim());
    try {
      await api("/api/me");
      location.href = "/";
    } catch {
      setErr("That access token didn't work. It's the JINAI_ADMIN_TOKEN value set on the server.");
    }
  }

  return (
    <>
      <TopBar />
      <form className="login" onSubmit={signIn}>
        <h1>Sign in</h1>
        <label style={{ display: "flex", flexDirection: "column", gap: 6, color: "var(--muted)", fontSize: 14 }}>
          Access token
          <input className="field" type="password" autoFocus value={token} onChange={(e) => setT(e.target.value)} />
        </label>
        {err && <p className="err">{err}</p>}
        <button className="btn primary" disabled={!token.trim()}>Sign in</button>
      </form>
    </>
  );
}
