# Usecase
This is 'Partner Radar', a tool to help recruiters track information about partners across various firms. This tool operates in three parts.

## Discovery
Recruiters are able to add firms, which are continuously monitored for changes to their partner roster. This will find if a partner has left a firm, joined a firm, or transferred. 

Adding firms can either be done by:
- Finding sitemaps of each firm, the profile pages and the CSS selectors for various fields
- An AI assisted setup, that just requires the website of the firm that will be added

Once firms have been added to the system, each found partner profile webpage is scraped and processed into a profile we store locally. There is a template of details which are sought, such as 'education', 'bar admissions' and 'languages', however due to the versatile database, we are also able to store additional details through configuration.

## Validation
Since Partner Radar relies heavily on AI and keyword search for parsing profile webpages, information can be validated by employees to ensure the information stored is up to date. Additionally, recruiters can add other important details using 'team notes', which can help notify colleagues about the status of the partner. Validation is additionally required to check if a partner has moved to another business.

## AI suggestions
There are 3 integrations of AI in this project:

- Call prep brief: This takes in all information we have found on the partner and creates an AI overview of the candidate.
- Market digest: This creates an AI overview of the recent moves, joins, leaves and promotions of partners.
- Organisation setup: Since most users will be non technical, the purpose of this integration is to make it easy for recruiters to add new firms to be monitored.

# Demo / Setup instructions

The demo is available at [Radar](https://radar.adam-french.co.uk) with login credentials given previously.

The hosted demo is recommended over running locally, since the data has to be crawled from the firms' websites and the AI features need an API key. A [manual setup guide](docs/SETUP.md) is available, however it may take around 30 minutes.

Watching the demo video could acquaint the user with most of the application:
[Demo video (8.5 min)](docs/demo.mp4)

# Techstack
- Frontend: React, served by Caddy
- Backend API: Python and FastAPI
- Backend Worker: Python, runs the crawling jobs
- Database: MongoDB
- AI: OpenRouter

# Architecture Decisions
- MongoDB: Each partner is one flexible document, so new details such as 'bar admissions' can be added through configuration without a schema change.
- Job queue: Stored in MongoDB rather than a separate service. Workers claim jobs with a lease, so a crashed job is retried.
- LLM gateway: Every AI call goes through one class that handles caching, a daily budget and cost logging.
- Sitemap scraping: Firms' sitemaps list every profile page, which is cheaper and more reliable than crawling the whole site.

# What's been left out
The UI isn't perfect, and would definitely need refining if this were moved to a potential product. There are various issues, as some paragraphs and information appear too dense as well as names of partners missing a space. These don't hinder the main objective of the demo and therefore haven't been addressed properly.

There could be further integrations of different sources. Currently, we are only using firms' sitemaps, however in the future, information could be added through social media APIs such as LinkedIn. This wasn't implemented because LinkedIn requires a strict business licence to scrape their social network, same for other networks.

Additional features are suggested for future implementation: 
- Recruiters having their own accounts to 'star' and track specific partners.
- Different permissions of user profiles, allowing some users only to read and admins to edit configs.
- Timelines to track a partner's application process to a new role.

# Time spent

The time spent on this project is approximate and is estimated as:
- 2 hours: Researching infrastructure decisions and research on how to extract data. After this, a prompt was assembled with a design specification of the project.
- 0 hours: AI implementation took place without supervision which I am not counting as time towards the project.
- 2 hours: Reviewing the codebase, stress testing, UI and bugfixes.
- 2 hours: Presenting work done neatly and configuring the demo.
