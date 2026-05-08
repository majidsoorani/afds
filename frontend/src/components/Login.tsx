import { useState, FormEvent } from "react";
import { Shield, Loader2 } from "lucide-react";
import { login } from "@/lib/auth";

interface Props {
  onSuccess: () => void;
}

export function Login({ onSuccess }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(username.trim(), password);
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center justify-center mb-6">
          <Shield className="h-10 w-10 text-primary" />
          <span className="ml-3 text-2xl font-semibold">AFDS</span>
        </div>
        <div className="rounded-lg border bg-card p-6 shadow-sm">
          <h1 className="text-lg font-semibold mb-1">Sign in</h1>
          <p className="text-sm text-muted-foreground mb-4">
            Enter your AFDS credentials to access the dashboard.
          </p>
          <form onSubmit={handleSubmit} className="space-y-3">
            <div>
              <label className="block text-sm mb-1" htmlFor="username">Username</label>
              <input
                id="username"
                type="text"
                autoComplete="username"
                autoFocus
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>
            <div>
              <label className="block text-sm mb-1" htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>
            {error && (
              <div className="rounded-md bg-destructive/10 text-destructive text-sm px-3 py-2">
                {error}
              </div>
            )}
            <button
              type="submit"
              disabled={loading}
              className="w-full rounded-md bg-primary text-primary-foreground py-2 text-sm font-medium hover:opacity-90 disabled:opacity-60 flex items-center justify-center"
            >
              {loading && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Sign in
            </button>
          </form>
        </div>
        <p className="text-xs text-muted-foreground text-center mt-4">
          Need access? Contact the AFDS admin.
        </p>
      </div>
    </div>
  );
}
