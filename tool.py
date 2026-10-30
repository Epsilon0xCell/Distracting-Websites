# I wrote this file. It judges websites against a task the user describes
# and prints a verdict. It does not block, redirect, or change anything on
# the machine, whatever the output says. Every score below comes from the
# Gemini model or from the fixed tables in this file, so a verdict is only
# as good as the page text I manage to fetch and the prompt I send with it.
import requests
from bs4 import BeautifulSoup
from google import genai
from urllib.parse import urlparse
from typing import List, Optional
import os
import re

# The Gemini client is created lazily so that importing this module (and
# running the parts that do not need the API) never crashes. Set the
# GEMINI_API_KEY environment variable (GOOGLE_API_KEY is also accepted).
_client = None

def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "No Gemini API key found. Set the GEMINI_API_KEY environment "
                "variable to a key from Google AI Studio and run again."
            )
        _client = genai.Client(api_key=api_key)
    return _client

def normalize_domain(website: str) -> str:
    # urlparse only fills netloc when a scheme is present, and callers type
    # addresses in forms like "khanacademy.org" or "www.khanacademy.org".
    candidate = website if "://" in website else "https://" + website
    domain = urlparse(candidate).netloc.lower().split(":")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain

def ensure_scheme(website: str) -> str:
    return website if "://" in website else "https://" + website

def parse_score(text: str) -> Optional[int]:
    # Models often answer "Score: 85" instead of a bare number. Take the
    # first number in the reply instead of requiring the whole reply to be
    # digits (int(text) raised ValueError, and joining every digit in the
    # reply concatenated unrelated numbers into a wrong score).
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    if not match:
        return None
    return int(float(match.group()))

class Question:
    def __init__(self, text: str):
        self.text = self.clean_question(text)
    
    @staticmethod
    def clean_question(text: str) -> str:
        # The model answers in markdown list form, so I strip the bold
        # markers and the "1. " style numbering here instead of trusting
        # every call site to clean up. The trade-off: the numbering pattern
        # also matches inside a question, so a question that mentions
        # something like "chapter 3. vectors" loses the "3. " in display.
        text = re.sub(r'\*\*|\d+\.\s+', '', text)
        return text.strip()
    
    def ask(self) -> str:
        # I let EOFError from input() propagate out of here on purpose.
        # main() is the one place that turns a closed stdin into a clean
        # exit, and catching it in every prompt method would duplicate that.
        print("\n" + "-" * 80)
        print(f"Question: {self.text}")
        print("-" * 80)
        return input("Your answer: ").strip()

def get_user_task():
    initial_task = input("Please enter the task you want to achieve: ")
    return ask_additional_questions(initial_task)

def get_specific_questions(task_details: str) -> List[Question]:
    prompt = f"""
    Current task details: "{task_details}"

    Check for REQUIRED information:
    1. Subject Area:
       - What specific subject?
       - Which specific topic/subtopic?
    2. Task Details:
       - What type of task?
       - Which specific problems/concepts?
    3. Context:
       - What is current knowledge level?
       - What are the learning goals?
    4. Resource Needs:
       - What type of help is needed?
       - Any specific format requirements?

    Generate questions for ALL missing required information.
    Ignore information we already have.
    Questions must be specific and build on previous answers.

    Format:
    MISSING_CATEGORIES: (list categories with missing info)
    QUESTIONS: (one question per line)
    """
    try:
        response = get_client().models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        response_text = response.text.strip()

        # Parse sections. The prompt above asks for MISSING_CATEGORIES: and
        # QUESTIONS: headers; the parser previously looked for MISSING:,
        # which the prompt never produces, so the nothing-missing check
        # below could never fire.
        # This parse only works if the model puts the headers at the start
        # of a line, the way the prompt shows them. If the model wraps the
        # reply in a code fence or indents the headers, no section is ever
        # opened and I return no questions, which the caller then replaces
        # with its fallback set. I accept that instead of guessing at
        # near-miss headers, because a misparsed reply once produced
        # questions the model never asked.
        sections = {}
        current_section = None
        for line in response_text.split('\n'):
            if line.startswith(('HAVE:', 'MISSING_CATEGORIES:', 'MISSING:', 'QUESTIONS:')):
                header = line.split(':')[0]
                current_section = 'MISSING' if header.startswith('MISSING') else header
                sections[current_section] = []
                remainder = line.split(':', 1)[1].strip()
                if remainder:
                    sections[current_section].append(remainder)
            elif current_section and line.strip():
                sections[current_section].append(line.strip())

        # If we have everything we need (no MISSING items), return no questions
        if 'MISSING' in sections and not sections['MISSING']:
            return []
            
        # Only return questions if we're missing critical information
        if 'QUESTIONS' in sections:
            return [Question(q) for q in sections['QUESTIONS'] if '?' in q]
        return []
        
    except Exception as e:
        print(f"Error generating questions: {str(e)}")
        return []

def is_task_specific_enough(task_details):
    prompt = f"""
    Analyze task details: {task_details}

    Required Information Check:
    1. Subject Area (0-25):
       - Specific subject identified? (15)
       - Specific topic/subtopic clear? (10)
    
    2. Task Details (0-25):
       - Clear task type (homework, study, practice)? (15)
       - Specific problem/concept identified? (10)
    
    3. Context (0-25):
       - Current knowledge/level clear? (10)
       - Learning goals identified? (15)
    
    4. Resource Needs (0-25):
       - Type of help needed clear? (15)
       - Format/requirements specified? (10)

    Score each category and sum total (0-100).
    Task is specific enough only if ALL categories have points.
    Respond with only the number.
    """
    try:
        response = get_client().models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        confidence = parse_score(response.text)
        if confidence is None:
            return False

        # The prompt asks for 20 or more in each of the four categories,
        # but the model returns one total number, so I cannot check the
        # per-category part from here. The 80 threshold is the only part
        # I actually enforce. A task strong in three categories and empty
        # in the fourth can pass if the total reaches 80, and I would
        # rather say that plainly than pretend the check is stricter.
        return confidence >= 80
    except Exception as e:
        print(f"Error checking task specificity: {str(e)}")
        return False

def ask_additional_questions(task_details: str) -> str:
    required_info_gathered = False
    iteration = 0
    max_iterations = 10  # Increased max iterations
    
    # The iteration cap is a cost bound, not a quality target. Each pass is
    # two model calls, and a user who answers vaguely forever would
    # otherwise loop until they kill the program. After 10 rounds I hand
    # back whatever detail has gathered and let the site checks run on it.
    while not required_info_gathered and iteration < max_iterations:
        if is_task_specific_enough(task_details):
            required_info_gathered = True
            break

        questions = get_specific_questions(task_details)
        if not questions:
            # If no questions but not specific enough, generate fallback questions
            questions = [
                Question("What specific subject and topic is this for?"),
                Question("What exactly do you need to do?"),
                Question("What kind of help do you need?")
            ]
            
        print("\nLet's get more specific details about your task:")
        answers = []
        for question in questions:
            answer = question.ask()
            if not answer:  # If user doesn't provide answer, stop asking
                # An empty answer ends the whole gathering phase, and the
                # answers collected in this round are dropped with it.
                # I treat a blank as "stop asking me", not as "skip this
                # one question", because that is how people use it.
                return task_details
            answers.append(f"{question.text}: {answer}")
        
        task_details = f"{task_details}\nAdditional Details: {'; '.join(answers)}"
        iteration += 1
    
    return task_details

def get_website_info(website):
    # These two platforms sit behind sign-in walls, so a fetch returns a
    # login page that says nothing about the real content. I answer from
    # this fixed profile instead of scoring the login page. This table is
    # separate from the scoring table in determine_relevance, and the two
    # have already drifted apart once, so a platform added to one place
    # still needs its entry in the other.
    known_edu_platforms = {
        'classroom.google.com': {
            'title': 'Google Classroom',
            'description': 'Educational learning management system for schools',
            'type': 'lms',
            'features': ['assignments', 'course materials', 'teacher communication']
        },
        'khanacademy.org': {
            'title': 'Khan Academy',
            'description': 'Free educational resources and practice exercises',
            'type': 'learning',
            'features': ['lessons', 'practice', 'videos']
        },
        # Add more known platforms as needed
    }

    # Check if it's a known platform first. This is an exact domain match
    # after normalization, so a subdomain like courses.khanacademy.org
    # does not hit the fast path and gets fetched like any unknown site.
    domain = normalize_domain(website)

    if domain in known_edu_platforms:
        platform = known_edu_platforms[domain]
        return "\n".join([
            f"Title: {platform['title']}",
            f"Description: {platform['description']}",
            f"Platform Type: {platform['type']}",
            f"Features: {', '.join(platform['features'])}",
            "Domain Analysis: educational: Yes",
            "Educational Platform: Yes",
            "Distraction Level: 0.1",  # Low distraction for dedicated edu platforms
            "Entertainment Elements: False",
            "Social Elements: Limited",
            "Advertisement Level: False"
        ])

    # Regular website analysis for unknown sites
    try:
        http_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        # I never check the status code here. A 404 or a block page is
        # parsed and scored like real content, which can produce a
        # confident verdict about a page the user never sees. The 10
        # second timeout is the only guard, and a slow site falls into
        # the exception path below and is reported as a fetch failure.
        response = requests.get(ensure_scheme(website), headers=http_headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')

        # Get all text content
        for script in soup(["script", "style"]):
            script.decompose()

        # Enhanced extraction of key information
        title = soup.title.string if soup.title else ""
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        # A description meta tag without a content attribute used to raise
        # KeyError here and discard the whole page extraction.
        description = meta_desc.get('content', '') if meta_desc else ""

        # Get main content safely
        headings = []
        try:
            headings = [h.text.strip() for h in soup.find_all(['h1', 'h2', 'h3']) if h.text.strip()][:5]
        except:
            headings = []
            
        paragraphs = []
        try:
            paragraphs = [p.text.strip() for p in soup.find_all('p') if p.text.strip()][:3]
        except:
            paragraphs = []
        
        # Domain analysis
        domain_indicators = {
            'educational': {
                'domains': ['classroom.google.com', 'khanacademy.org', 'quizlet.com', 
                          'education', 'learn', 'study', 'school', 'academic', 'math'],
                'keywords': ['math', 'learn', 'course', 'education', 'study', 'practice']
            },
            'professional': {
                'domains': ['github.com', 'gitlab.com', 'bitbucket.org', 'jira.com', 'confluence', 'docs.'],
                'keywords': ['documentation', 'repository', 'project', 'workflow', 'professional', 'enterprise']
            },
            'development': {
                'domains': ['stackoverflow.com', 'developer.', 'docs.', '.dev', 'api.'],
                'keywords': ['code', 'programming', 'developer', 'api', 'documentation', 'framework']
            },
            'creative': {
                'domains': ['behance.net', 'dribbble.com', 'adobe.', 'figma.com', 'canva.com'],
                'keywords': ['design', 'creative', 'artwork', 'portfolio', 'sketch', 'prototype']
            },
            'productivity': {
                'domains': ['notion.so', 'trello.com', 'asana.com', 'monday.com', 'clickup.com'],
                'keywords': ['task', 'project', 'organize', 'plan', 'collaborate', 'manage']
            }
        }

        # Safe domain checking. Unlike the platform checks above, this one
        # is a plain substring test on the raw text the user typed, so a
        # lookalike address that contains a listed name counts as a match.
        # It only feeds the summary text sent to the model, never a
        # verdict on its own, which is why I left the weaker check here.
        domain_matches = {}
        try:
            for domain_type, indicators in domain_indicators.items():
                domain_match = any(d in website.lower() for d in indicators['domains'])
                keyword_match = any(word in str(soup).lower() for word in indicators['keywords'])
                domain_matches[domain_type] = {
                    'domain_match': domain_match,
                    'keyword_match': keyword_match
                }
        except:
            domain_matches = {'error': {'domain_match': False, 'keyword_match': False}}

        # Add distraction indicators
        distraction_indicators = {
            'entertainment': ['video', 'gaming', 'play', 'watch', 'stream', 'subscribe', 'channel'],
            'social': ['share', 'like', 'comment', 'follow', 'social', 'friend', 'community'],
            'ads': ['advertisement', 'sponsored', 'promotion', 'buy', 'shop', 'product'],
            'recommendations': ['recommended', 'suggested', 'trending', 'popular', 'for you']
        }
        
        # Count distracting vs productive elements
        distraction_count = 0
        total_elements = 0
        page_text = str(soup).lower()
        
        for category, keywords in distraction_indicators.items():
            for keyword in keywords:
                # page_text is the whole HTML source, markup included, so
                # a keyword inside a tag attribute or a class name counts
                # the same as one in visible text. The level is a rough
                # signal for the model, not a measurement of the page a
                # reader sees.
                distraction_count += page_text.count(keyword)
                total_elements += 1
        
        # Calculate distraction ratio. total_elements counts keyword types,
        # not occurrences, so the raw value can exceed 1 (a page repeating
        # "video" 100 times scored 4.00). Cap it so the level stays a ratio.
        distraction_ratio = min(distraction_count / max(total_elements, 1), 1.0)
        
        # Safer content summary construction
        content_parts = [
            f"Title: {title[:500] if title else 'No title'}",
            f"Description: {description[:500] if description else 'No description'}",
            "Headers: " + " | ".join(headings) if headings else "No headers found",
            "Content: " + " ".join(paragraphs) if paragraphs else "No content found",
            "Domain Analysis: " + ", ".join([
                f"{domain}: {'Yes' if matches['domain_match'] or matches['keyword_match'] else 'No'}"
                for domain, matches in domain_matches.items()
            ]),
            f"Distraction Level: {distraction_ratio:.2f}",
            f"Entertainment Elements: {any(word in page_text for word in distraction_indicators['entertainment'])}",
            f"Social Elements: {any(word in page_text for word in distraction_indicators['social'])}",
            f"Advertisement Level: {any(word in page_text for word in distraction_indicators['ads'])}"
        ]
        
        return "\n".join(content_parts)
        
    except Exception as e:
        print(f"Warning: Limited website info available - {str(e)}")
        return f"URL: {website}\nNote: Could not fetch complete information"

def determine_relevance(task_details, website):
    # Known educational platform scoring
    known_edu_platforms = {
        'classroom.google.com': 90,  # Very high base score for LMS
        'khanacademy.org': 85,
        'quizlet.com': 80,
        'desmos.com': 85,
        'purplemath.com': 80,
        'wolframalpha.com': 85,
        'ixl.com': 80
    }
    
    domain = normalize_domain(website)

    # Special handling for known educational platforms
    if domain in known_edu_platforms:
        base_score = known_edu_platforms[domain]
        # Only adjust score down if task is clearly unrelated to platform's purpose
        if "math" in task_details.lower() and domain in ['classroom.google.com', 'khanacademy.org']:
            return True
        # Every score in the table above is 80 or higher, so this line
        # always returns True as the table stands. The comparison only
        # starts to mean something if someone adds a platform below 75,
        # which is the honest way to read it: this is a whitelist with a
        # dormant threshold, not a graded score.
        return base_score >= 75

    # Regular website analysis
    website_info = get_website_info(website)
    # The "URL:" prefix is the failure string get_website_info returns
    # when a fetch fails, so this check depends on that exact wording.
    # If the fallback text ever changes, failed fetches would be sent to
    # the model as if they were page summaries.
    if not website_info or website_info.startswith("URL:"):
        return False

    # High-risk sites that require extra scrutiny
    high_distraction_domains = {
        'youtube.com': 0.8,  # Needs 80% confidence to be considered productive
        'facebook.com': 0.9,
        'twitter.com': 0.9,
        'instagram.com': 0.9,
        'tiktok.com': 0.9,
        'reddit.com': 0.85
    }
    
    threshold_modifier = 1.0
    for high_domain, required_confidence in high_distraction_domains.items():
        # Match the domain itself or a subdomain of it, not any URL that
        # merely contains the text (for example notyoutube.com).
        if domain == high_domain or domain.endswith('.' + high_domain):
            threshold_modifier = required_confidence
            break

    prompt = f"""
    Task Requirements:
    {task_details}
    
    Website Analysis:
    {website_info}
    
    Evaluate this website carefully considering:
    
    1. Content Assessment (0-40 points):
    - Directly relevant content availability (+20)
    - Content quality and accuracy (+10)
    - Appropriate difficulty level (+10)
    
    2. Distraction Analysis (0-30 points):
    - Low presence of unrelated content (+10)
    - Minimal advertisements/promotions (+10)
    - Focus on educational/productive content (+10)
    
    3. Usability for Task (0-30 points):
    - Easy access to relevant content (+10)
    - Clear navigation to needed materials (+10)
    - Efficient use of time (+10)
    
    Important Rules:
    - Websites with high distraction ratios need exceptional educational value
    - Entertainment/social platforms must demonstrate clear educational focus
    - Consider the ratio of relevant vs irrelevant content
    - Account for potential time waste on unrelated content
    
    Respond with only a number 0-100 representing overall productivity score.
    """
    
    try:
        response = get_client().models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        base_confidence = parse_score(response.text)
        if base_confidence is None:
            return False
        
        # Apply threshold modifier for high-distraction sites
        final_confidence = base_confidence * threshold_modifier
        
        return final_confidence >= 75  # Increased base threshold
    except Exception as e:
        # Any model or network failure lands here and reads as "not
        # productive". I fail closed on purpose: a site I could not judge
        # should not get a yes from a crash.
        print(f"Error determining relevance: {e}")
        return False

def main():
    print("\n" + "=" * 80)
    print("Website Productivity Analyzer".center(80))
    print("=" * 80 + "\n")

    try:
        # Fail fast with a clear message when no API key is configured,
        # instead of crashing on the first model call. This call also has
        # to come first for a second reason: it builds the shared client
        # every later function reaches through get_client(), so the key
        # check and the setup stay in one place.
        get_client()
        task = get_user_task()
    except RuntimeError as e:
        print(str(e))
        return
    except (EOFError, KeyboardInterrupt):
        print("\nExiting.")
        return
    while True:
        try:
            website = input("\nEnter the website URL to analyze (or 'exit' to quit): ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break
        if website.lower() in ['exit', 'quit']:
            break
        print("\n" + "-" * 80)
        print(f"Analyzing {website} for productivity...")
        print("-" * 80)
        if determine_relevance(task, website):
            print(f"\n✅ {website} is productive for your task.")
        else:
            print(f"\n❌ {website} is not productive for your task.")
        print("\n" + "=" * 80)

if __name__ == "__main__":
    main()
