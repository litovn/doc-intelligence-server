# 1. Current Workflow:
> How do you currently use AI tools (e.g., ChatGPT, Cursor, Claude, MCPs, Agents) to assist or generate code? Do you use them differently for AI-specific tasks (e.g., designing a RAG pipeline) versus general backend work?

I use AI to both assist and generate code, but I stay resposible for everything it produces. I am the one who makes the decisions, the AI just does most of the typing. For multi-file changes I use plan mode, I explore the idea first, then plan the foundation and only then write the code, one topic per session. </br>
I never blindly rely on the model, I question every draft. It's about understanding why each implementation choice was made and ask for explanations, not delegate the code.

General backend and frontend work is well documented online, templates and reference implementations are easily accessible, and easy to review them through testing and just by running them. </br>
AI-specific tasks is where the model guesses and the fact that it runs, doesn't mean it gives godd answers. Building a prototype is easy, measuring if it performs well is where the attention is needed. That is why it is important to create and run comprehensive evaluations by defining what a correct answer is and make decisions based on the actual numbers.


# 2. The Good & The Bad:
> Where do you see the biggest value in AI-assisted development? What are the current limitations or risks, especially when building systems that will themselves be consumed by AI agents?

I see value in a couple of things. First, you are able to learn faster when the model explains things, rather than relying on the model to think for you. Second, speed: writing code from scratch or deploying in hours what used to take weeks. But I believe the biggest gain is coverage. You can rely on AI to do tasks that would otherwise be skipped, such as testing every important setting and parameter for a specific use case instead of settling for common defaults.

While I did say speed is a value, it may also feel like a limitation. The bottleneck moved from the coding phase to the review and production phase, taking more time for this stage than it used to. Writing new code from scratch is faster, but mature codebases need a different approach. </br>
Furthermore, models tend to sound sure about their answers without evidence to back it up. Unfortunatelly, while models are getting smarter, the code they write is not getting more secure at the same pace and AI-generated code introduces serious exploitable vulnerabilities ([*TRIDENT*](https://github.com/litovn/trident) was created because of this).

AI agents use every permission they're given, so it's really important to define their scope carefully. A poorly scoped agent can delete and recreate an entire environment and cause serious damage to a company, simply because it was able to.
Testing is essential, being non-deterministic, one success does not measure reliability.  A system built for agents has to be robust and has to be tested with agents in the loop over repeated runs. 


# 3. The Future:
> How do you envision your role as an AI Product Engineer evolving over the next few years? What skills do you think will matter most as LLMs get better at writing code?

I believe that both the models and agents will continue to improve. The goal will remain to get leverage from the use of agents without any compromise on the quality of the software. The role of an AI Product Engineer will shift towards orchestrating coding agents and overseeing their work, and the needed engineering skill will evolve to adapt to it, from writing code, to specyfing it and veryfying it. 

The problem will no longer be implementing a specification but deciding what to build. The skill that will grow the most will be knowing how to shape the product, so product sense, business context and customer goals will become core skill every AI Product Engineer should have. At the same time, the engineer has to own what the  model can't be held accountable for: security, access management, data handing and cost, will become core skills an engineer should develop and become an expert in.
Other skills that will matter are: 
- Making AI applications measurable, reading failures one by one and telling a real improvement apart from noise.
- Be knowledgable over the domain you are working in. 
- Reading and debugging code, a skill that mattered from the beggining of programming, but now is more relevant and important than ever.
