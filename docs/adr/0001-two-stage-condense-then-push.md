# Condense raw files to 1 s Summary files first, push to PI as a separate stage

The PI archiver accepts at most 1 s samples and the Backlog is huge, so stage 1 condenses Raw files into local Summary files and a quality report, and stage 2 pushes those files to PI. We chose this over a single read-condense-push pass so a PI outage or a changed statistic never forces re-reading hundreds of thousands of files from the slow storage share, and so the owner can judge the values before anything reaches PI.
