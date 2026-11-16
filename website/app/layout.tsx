import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Discord Member Verification", description: "Authorize and join a Discord community" };
export default function RootLayout({children}:{children:React.ReactNode}) { return <html lang="en"><body>{children}</body></html>; }