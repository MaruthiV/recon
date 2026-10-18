"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { api } from "@/lib/api";

export default function Login() {
  const router = useRouter();
  const [user, setUser] = useState("maruthi");
  const [tenant, setTenant] = useState("t_acme");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    try {
      await api("/v1/login", { method: "POST", body: JSON.stringify({ user, tenant_id: tenant }) });
      router.push("/");
    } catch (err) {
      setError(String(err));
    }
  }

  return (
    <div className="panel" style={{ maxWidth: 420 }}>
      <h1>Sign in</h1>
      <p className="muted">Dev login stub: any name, one tenant. Real auth comes in M4.</p>
      <form onSubmit={submit} style={{ display: "grid", gap: 10 }}>
        <label>
          Name
          <input value={user} onChange={(e) => setUser(e.target.value)} />
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
