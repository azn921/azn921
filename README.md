# ruvBot v0.01c

Created by [@rUv](https://github.com/ruvnet)

`ruvBot` is an advanced AI-powered chatbot based on the LlamaIndex framework, enhanced with Next.js for web application development. It's designed to interact intelligently with users, providing information and assistance across various domains.

## Key Features

- **Streaming Output**: Delivers real-time responses from the AI, enhancing user interaction.
- **Chat Memory**: Maintains conversation context across sessions, allowing for more meaningful and continuous interactions.
- **OpenAI Assistants API**: Leverages the power of OpenAI's language models for high-quality, context-aware responses.
- **Custom Knowledge Base**: Users can upload their own knowledge files to `/data` directory, allowing ruvBot to provide specialized information.
- **Versatile Applications**: Ideal for knowledge management, customer support, educational assistance, and more.

## Potential Uses

- **Knowledge Management**: Helps in organizing and retrieving information efficiently.
- **Customer Support**: Provides automated, yet personalized, responses to customer queries.
- **Educational Tool**: Assists in learning and tutoring across various subjects.
- **Content Exploration**: Aids in navigating through large documents or datasets, offering insights and summaries.

## Getting Started

### Prerequisites

- Node.js
- Access to a key-value store for chat history (e.g., Replit Database).
- OpenAI API Key (GPT-3).

### Installation

Clone the repository and install dependencies:
```bash
git clone https://github.com/ruvnet/ruvbot
cd ruvBot
npm install
```

### Configuration

1. Set up environment variables for database access and OpenAI API key.
2. Upload documents to `/data` to customize the bot's knowledge base.

### Running the Bot

Start the ruvbot
```bash
./run.sh
```

Access the bot at [http://localhost:3000](http://localhost:3000).

## Customization

Modify the welcome message and internal prompts to tailor the bot's interaction style and responses:

- **Welcome Message**: Edit `/components/ui/chat-section.tsx`.
- **Internal Prompts**: Adjust in `route.ts`.

## About LlamaIndex

LlamaIndex is an innovative framework designed to streamline the integration of AI models into applications. It simplifies the process of connecting with AI services like OpenAI, providing a seamless development experience for creating AI-powered applications.

## Contributions

We welcome your feedback and contributions. Enhance ruvBot by submitting issues and pull requests on our [GitHub repository](https://github.com/ruvnet/ruvbot).

## Congress.gov Legislative Data Extractor (Python)

This repo includes a Python CLI that extracts, normalizes, and stores legislative data from the official Congress.gov API (`https://api.congress.gov/v3`) for lobbying/policy tracking.

### Setup

- Set your API key as an environment variable:

```bash
export CONGRESS_API_KEY="YOUR_KEY_HERE"
```

- Install Python dependencies:

```bash
pip install -r requirements.txt
```

### Run

- Fetch specific bills:

```bash
python congress_gov_service.py --congress 119 --bill HR1234 --bill S.567
```

- Discover bills by keyword search (e.g. NDAA/appropriations):

```bash
python congress_gov_service.py --congress 119 --search "NDAA" --max-bills 50
```

- Optional committee/member exports:

```bash
python congress_gov_service.py --committee "Armed Services" --committee-chamber house
python congress_gov_service.py --member "Schumer"
```

- Fast mode (skips member enrichment):

```bash
python congress_gov_service.py --search "appropriations" --json-only
```

### Outputs

Files are saved under `./data/` (deduped across runs by `bill_id`):

- `data/bills_raw.json`
- `data/bills_normalized.json`
- `data/committees.json`
- `data/members.json`