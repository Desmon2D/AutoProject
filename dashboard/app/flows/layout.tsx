import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Flow Builder — Automation Control",
  description: "Визуальный редактор автоматизаций и агентных workflow.",
};

export default function FlowsLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
