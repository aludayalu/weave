import { Geist, Geist_Mono } from "next/font/google";
import "../globals.css";
import "streamdown/styles.css";
import Base from "./base";

const geistSans = Geist({
    variable: "--font-geist-sans",
    subsets: ["latin"],
});

const geistMono = Geist_Mono({
    variable: "--font-geist-mono",
    subsets: ["latin"],
});

export const metadata = {
    title: "Chat",
    description: "Lumatozer Chat",
};

export default function RootLayout({ children }) {
    return (
        <html
            lang="en"
            className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
        >
            <body className="h-full overflow-hidden flex flex-col">
                <Base>{children}</Base>
            </body>
        </html>
    )
}