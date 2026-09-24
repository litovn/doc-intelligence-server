import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Doc Intelligence",
  description: "Knowledge-base document manager",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
