## GOAL:

Your goal is to create a Prisma Checker to run in a dir.

UI:

- The UI will list the dirs that have prisma schema as List
- There will be indicators:
    - Does this dir has DB addr or not
    - Is this currently turned on or not (details are coming below)
    - Start, Stop buttons
    - How much Time passed of being live
    - URL Path // Open in Browser button
- Settings:
    - Change / update file path pattern
    - Upgrade Run command (default: `npx prisma@6 studio  --schema {{PathOfSchema}} --browser none`)
    - Should Auto open in browser or not.

- Clicking on a List item will expand detailed section for that schema

The project will:

- Scan all the subdir's
- Check for a pattern (default: "prisma/schema.prisma"), which can be modified from settings or multiple can be added
- Check for that dir's ".env" and check if the variable for database in schema.prisma has db value on .env
- It will open prisma studio (Maybe more commands coming in-future)
