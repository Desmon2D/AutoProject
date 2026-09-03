import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin", "cyrillic"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin", "cyrillic"] });

export async function generateMetadata(): Promise<Metadata> {
  const incoming = await headers();
  const host = incoming.get("host") ?? "127.0.0.1:4173";
  const protocol = incoming.get("x-forwarded-proto") ?? "http";
  const image = `${protocol}://${host}/og.png`;
  return {
    title: "Automation Control — Agent Operations",
    description: "Локальный центр мониторинга сценариев, шагов и агентных процессов.",
    openGraph: {
      title: "Automation Control",
      description: "Agent Operations Dashboard",
      images: [image],
    },
    twitter: { card: "summary_large_image", images: [image] },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body className={`${geistSans.variable} ${geistMono.variable}`}>{children}</body>
    </html>
  );
}
