# Telegram news monitor — გიორგი გახარია / პარტია საქართველოსთვის

ეს არის გამზადებული MVP, რომელიც:
- ამოწმებს ნიუსებს Google News RSS ძიებით
- ეძებს ქივორდებს: `გიორგი გახარია`, `პარტია საქართველოსთვის`, `For Georgia`
- დუბლიკატებს აღარ აგზავნის
- ახალ მასალას ავტომატურად დებს Telegram channel-ში
- გაშვებადია GitHub Actions-ით ყოველ 30 წუთში

## რა არის უკვე გამზადებული
- `src/main.py` — მთავარი სკრიპტი
- `.github/workflows/news-monitor.yml` — ავტომატური გაშვების workflow
- `.env.example` — შესავსები კონფიგურაცია
- `seen.db` — პირველად გაშვებისას თვითონ შეიქმნება

## არხის აწყობის გეგმა

### 1) Telegram channel შექმნა
- შექმენი ახალი public ან private channel
- სასურველია public იყოს, თუ გინდა ხალხმა გამოიწეროს
- დაიმახსოვრე username, მაგალითად: `@gakharia_alerts`

### 2) Bot შექმნა
- Telegram-ში გახსენი `@BotFather`
- გაუშვი `/newbot`
- დაარქვი სახელი, მაგალითად `Georgia Politics Alerts`
- მიიღებ bot token-ს

### 3) Bot-ის დამატება channel-ში
- შედი channel-ის settings-ში
- დაამატე bot როგორც admin
- მიეცი უფლება `Post Messages`

### 4) GitHub-ზე ატვირთვა
- შექმენი ახალი GitHub repository
- ატვირთე ეს ფაილები

### 5) GitHub Secrets დაყენება
Repository → Settings → Secrets and variables → Actions → New repository secret

დაამატე ეს secret-ები:
- `TELEGRAM_BOT_TOKEN` → BotFather-იდან მიღებული token
- `TELEGRAM_CHANNEL_ID` → მაგალითად `@gakharia_alerts`
- `KEYWORDS` → `გიორგი გახარია;პარტია საქართველოსთვის;For Georgia`
- `CHECK_WINDOW_DAYS` → `7`
- `MAX_POSTS_PER_RUN` → `10`
- `LANGUAGE` → `ka`
- `COUNTRY` → `GE`

### 6) ტესტი
- GitHub-ში შედი Actions tab-ში
- გაუშვი `Telegram News Monitor` ხელით (`Run workflow`)
- თუ ყველაფერი სწორად არის, channel-ში დაიდება პოსტი

## როგორ მუშაობს ძიება
სკრიპტი ერთდროულად რამდენიმე search query-ს ამოწმებს, რომ უკეთ დაიჭიროს:
- ზუსტი ქივორდები
- Georgia politics კონტექსტი
- რამდენიმე ქართული მედიის site-ებით გაძლიერებული ძიება

## როგორ შეცვალო ქივორდები
`KEYWORDS` secret-ში ჩაწერე `;`-ით გაყოფილი სია.
მაგალითად:
`გიორგი გახარია;პარტია საქართველოსთვის;ანა დოლიძე`

## როგორ შეცვალო სიხშირე
ფაილი:
`.github/workflows/news-monitor.yml`

ამ ხაზში:
```yaml
- cron: '*/30 * * * *'
```

ეს ნიშნავს ყოველ 30 წუთში ერთხელ.

მაგალითები:
- ყოველ 15 წუთში: `*/15 * * * *`
- ყოველ საათში: `0 * * * *`

## რა აქვს ამ MVP-ს შეზღუდვა
- ზოგჯერ ერთი და იგივე თემა სხვადასხვა სტატიად მოვა
- summary არის feed-იდან აღებული მოკლე ტექსტი, არა სრულფასოვანი AI რეზიუმე
- წყაროების სიზუსტე დამოკიდებულია RSS ძიების შედეგებზე

## შემდეგი გაუმჯობესება
თუ გინდა, შემდეგ ვერსიაში შეიძლება დაემატოს:
- კონკრეტული ქართული მედიის ცალკე parser-ები
- უკეთესი deduplication
- AI summary
- თემების tagging
- web dashboard / archive
