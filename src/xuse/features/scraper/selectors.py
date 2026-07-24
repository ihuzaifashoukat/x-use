THREAD_INDICATORS = [
    r"\(\d+/\d+\)",
    r"\d+/\d+",
    "thread",
    "🧵",
    r"1\.",
    r"a\.",
    r"i\.",
]

# XPath/CSS selectors used by the scraper
X_TWEET_ARTICLE_XPATH = "//article[@data-testid='tweet']"
X_USER_NAME_XPATH = "//div[@data-testid='User-Name']//span[1]//span"
X_USER_HANDLE_XPATH = "//div[@data-testid='User-Name']//span[contains(text(), '@')]"
X_TWEET_TEXT_XPATH = "//div[@data-testid='tweetText']//span | //div[@data-testid='tweetText']//a"
X_STATUS_LINK_XPATH = "//a[contains(@href, '/status/') and .//time]"
X_TIME_TAG = ".//time"
X_ENGAGEMENT_BUTTON_XPATH = (
    "//button[@data-testid='{testid}']//span[@data-testid='app-text-transition-container']//span"
)
X_ANALYTICS_VIEW_XPATH = (
    "//a[contains(@href, '/analytics')]//span[@data-testid='app-text-transition-container']//span"
)
X_HASHTAG_LINKS_XPATH = "//a[contains(@href, 'src=hashtag_click')]"
X_MENTION_LINKS_XPATH = "//div[@data-testid='tweetText']//a[contains(text(), '@')]"
X_PROFILE_IMG_XPATH = "//div[@data-testid='Tweet-User-Avatar']//img"
X_MEDIA_IMAGE_XPATH = "//div[@data-testid='tweetPhoto']//img"
X_MEDIA_VIDEO_XPATH = "//div[contains(@data-testid, 'videoPlayer')]//video"
X_MEDIA_XPATH = f"{X_MEDIA_IMAGE_XPATH} | {X_MEDIA_VIDEO_XPATH}"  # back-compat union
X_VERIFIED_ICON_SVG = "//*[local-name()='svg' and @data-testid='icon-verified']"

