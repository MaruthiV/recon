"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { api } from "@/lib/api";

export default function Login() {
  const router = useRouter();
  const [user, setUser] = useState("operator");
  const [password, setPassword] = useState("");
  const [tenant, setTenant] = useState("t_acme");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api("/v1/login", { method: "POST", body: JSON.stringify({ user, password, tenant_id: tenant }) });
      router.push("/");
    } catch {
      setError("Wrong name or password.");
    }
  }

  return (
    <div className="panel" style={{ maxWidth: 420 }}>
      <h1>Sign in</h1>
      <p className="muted">
        Local demo users are <code>operator</code> and <code>supervisor</code> (only a supervisor can approve refunds).
        The password is <code>RECON_DEMO_PASSWORD</code>, see <code>.env.example</code>.
      </p>
      <form onSubmit={submit} style={{ display: "grid", gap: 10 }}>
        <label>
          Name
          <input value={user} onChange={(e) => setUser(e.target.value)} autoComplete="username" />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        <label>
          Tenant
          <input value={tenant} onChange={(e) => setTenant(e.target.value)} />
        </label>
        <button className="primary" type="submit">
          Sign in
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  );
}
