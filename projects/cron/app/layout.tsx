import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
});

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
});

export const metadata: Metadata = {
  metadataBase: new URL('http://localhost:3000'),
  title: 'AI Cron — AI-задачи по расписанию',
  description: 'Локальный центр управления автоматическими AI-задачами.',
  openGraph: {
    title: 'AI Cron',
    description: 'AI-задачи по расписанию',
    images: ['/og.png'],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'AI Cron',
    description: 'AI-задачи по расписанию',
    images: ['/og.png'],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ru">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
