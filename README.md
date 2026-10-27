## 🎥 Demo

See Literature Review Copilot in action:

[▶ Watch the Literature Review Copilot demo](demo/literature-review-demo.mov)
# Literature Review Copilot

## Features

### 📚 Structured Research Library

Organise literature hierarchically by research topic and document type.

The library supports structures such as:

```text
Research Topic
├── Articles
│   ├── Paper 1
│   └── Paper 2
├── Books
└── Theses
```

Folders and documents remain collapsed until selected, allowing large literature collections to remain manageable.

### 🔎 Literature Search and Navigation

Search and navigate individual papers without manually scrolling through a large document collection.

The interface provides structured access to individual sources while preserving their organisation within the research library.

### 📄 Document-Level Analysis

Select individual academic sources for focused analysis rather than treating the literature collection as a single undifferentiated dataset.

This makes it possible to move between collection-level exploration and close analysis of individual papers.

### 🤖 AI-Assisted Research

The application integrates large language models to support literature analysis and research workflows.

Current integrations include:

- Anthropic Claude
- OpenAI models

API credentials are loaded through environment variables and are **not stored in the repository**.

Users must provide their own API credentials.

### 📚 Zotero Integration

Zotero API integration allows the application to connect literature-management workflows with an existing research library.

### 🔐 Local Research Data

Research databases, uploaded literature, API credentials, and other private research data are intentionally excluded from the public repository.

This allows the application code to be shared without publishing the underlying research materials.

## Technology Stack

- Python
- Streamlit
- SQLite
- OpenAI API
- Anthropic API
- Zotero API
- pypdf
- Environment-variable based credential management

## Architecture

```text
Literature Review Copilot
        │
        ├── Research Library
        │      ├── Topics
        │      ├── Articles
        │      ├── Books
        │      └── Theses
        │
        ├── Literature Search
        │
        ├── Document Analysis
        │
        ├── Zotero Integration
        │
        └── AI Layer
               ├── Claude
               └── OpenAI
```

## Installation

Clone the repository:

```bash
git clone https://github.com/askariankimia-dotcom/literature-review-copilot.git
```

Move into the project directory:

```bash
cd literature-review-copilot
```

Create a virtual environment:

```bash
python3 -m venv .venv
```

Activate it on macOS/Linux:

```bash
source .venv/bin/activate
```

Install the required dependencies:

```bash
pip install -r requirements.txt
```

## API Configuration

Create a `.env` file in the project directory.

For example:

```text
OPENAI_API_KEY=your_openai_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
ZOTERO_API_KEY=your_zotero_api_key
ZOTERO_USER_ID=your_zotero_user_id
```

**Never commit your `.env` file to GitHub.**

The repository's `.gitignore` excludes environment files, local databases, and other private research data.

## Running the Application

Activate the virtual environment and run:

```bash
streamlit run app.py
```

Then open the local address displayed by Streamlit in your browser.

## Privacy and Research Data

This repository contains the application source code but does **not** contain the author's research database or API credentials.

The following are intentionally excluded:

- API keys
- `.env` files
- local SQLite databases
- personal literature collections
- research datasets

Researchers using the application should provide their own literature collection and API credentials.

## Project Status

Literature Review Copilot is an active research-software project and continues to evolve alongside real literature-review workflows.

The project explores how domain knowledge, research-software development, and AI-assisted tools can be combined to support academic research more effectively.

## Author

**Kimia Askarian**

Researcher in linguistics and corpus-based language research.

GitHub: [askariankimia-dotcom](https://github.com/askariankimia-dotcom)