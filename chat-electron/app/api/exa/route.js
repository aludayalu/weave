export const runtime = "edge";

export async function POST(req) {
    const { apiKey, query, numResults = 5 } = await req.json();

    const response = await fetch("https://api.exa.ai/search", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "x-api-key": apiKey,
        },
        body: JSON.stringify({
            query,
            type: "auto",
            numResults,
            contents: {
                text: true,
            },
        }),
    });

    const text = await response.text();

    return new Response(text, {
        status: response.status,
        headers: {
            "Content-Type": "application/json",
        },
    });
}