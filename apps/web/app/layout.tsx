import type { ReactNode } from "react";
import "./globals.css";

export const metadata = { title: "Recon", description: "payment-ops console (synthetic sandbox)" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <a href="/" className="brand">
            Recon
          </a>
          <span className="muted">synthetic sandbox, no real money</span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
