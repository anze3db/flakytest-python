""

env_vars = [
    "CI",
    # GITHUB ACTIONS
    "GITHUB_ACTION",
    "GITHUB_ACTION_PATH",
    "GITHUB_ACTION_REPOSITORY",
    "GITHUB_ACTIONS",
    "GITHUB_ACTOR",
    "GITHUB_ACTOR_ID",
    "GITHUB_API_URL",
    "GITHUB_BASE_REF",
    "GITHUB_ENV",
    "GITHUB_EVENT_NAME",
    "GITHUB_EVENT_PATH",
    "GITHUB_GRAPHQL_URL",
    "GITHUB_HEAD_REF",
    "GITHUB_JOB",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_REF",
    "GITHUB_REF_NAME",
    "GITHUB_REF_PROTECTED",
    "GITHUB_REF_TYPE",
    "GITHUB_REPOSITORY",
    "GITHUB_REPOSITORY_ID",
    "GITHUB_REPOSITORY_OWNER",
    "GITHUB_REPOSITORY_OWNER_ID",
    "GITHUB_RETENTION_DAYS",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_NUMBER",
    "GITHUB_SERVER_URL",
    "GITHUB_SHA",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_WORKFLOW",
    "GITHUB_WORKFLOW_REF",
    "GITHUB_WORKFLOW_SHA",
    "GITHUB_WORKSPACE",
    "RUNNER_ARCH",
    "RUNNER_DEBUG",
    "RUNNER_NAME",
    "RUNNER_OS",
    "RUNNER_TEMP",
    "RUNNER_TOOL_CACHE",
    # GITLAB
    "CHAT_CHANNEL",
    "CHAT_INPUT",
    "CHAT_USER_ID",
    "CI_API_V4_URL",
    "CI_API_GRAPHQL_URL",
    "CI_BUILDS_DIR",
    "CI_COMMIT_AUTHOR",
    "CI_COMMIT_BEFORE_SHA",
    "CI_COMMIT_BRANCH",
    "CI_COMMIT_DESCRIPTION",
    "CI_COMMIT_MESSAGE",
    "CI_COMMIT_REF_NAME",
    "CI_COMMIT_REF_PROTECTED",
    "CI_COMMIT_REF_SLUG",
    "CI_COMMIT_SHA",
    "CI_COMMIT_SHORT_SHA",
    "CI_COMMIT_TAG",
    "CI_COMMIT_TAG_MESSAGE",
    "CI_COMMIT_TIMESTAMP",
    "CI_COMMIT_TITLE",
    "CI_CONCURRENT_ID",
    "CI_CONCURRENT_PROJECT_ID",
    "CI_CONFIG_PATH",
    "CI_DEBUG_TRACE",
    "CI_DEBUG_SERVICES",
    "CI_DEFAULT_BRANCH",
    "CI_DEPENDENCY_PROXY_GROUP_IMAGE_PREFIX",
    "CI_DEPENDENCY_PROXY_DIRECT_GROUP_IMAGE_PREFIX",
    "CI_DEPENDENCY_PROXY_SERVER",
    "CI_DEPENDENCY_PROXY_USER",
    "CI_DEPLOY_FREEZE",
    "CI_DEPLOY_PASSWORD",
    "CI_DEPLOY_USER",
    "CI_DISPOSABLE_ENVIRONMENT",
    "CI_ENVIRONMENT_NAME",
    "CI_ENVIRONMENT_SLUG",
    "CI_ENVIRONMENT_URL",
    "CI_ENVIRONMENT_ACTION",
    "CI_ENVIRONMENT_TIER",
    "CI_RELEASE_DESCRIPTION",
    "CI_GITLAB_FIPS_MODE",
    "CI_HAS_OPEN_REQUIREMENTS",
    "CI_JOB_ID",
    "CI_JOB_IMAGE",
    "CI_JOB_MANUAL",
    "CI_JOB_NAME",
    "CI_JOB_NAME_SLUG",
    "CI_JOB_STAGE",
    "CI_JOB_STATUS",
    "CI_JOB_TIMEOUT",
    "CI_JOB_TOKEN",
    "CI_JOB_URL",
    "CI_JOB_STARTED_AT",
    "CI_KUBERNETES_ACTIVE",
    "CI_NODE_INDEX",
    "CI_NODE_TOTAL",
    "CI_OPEN_MERGE_REQUESTS",
    "CI_PAGES_DOMAIN",
    "CI_PAGES_URL",
    "CI_PIPELINE_ID",
    "CI_PIPELINE_IID",
    "CI_PIPELINE_SOURCE",
    "CI_PIPELINE_TRIGGERED",
    "CI_PIPELINE_URL",
    "CI_PIPELINE_CREATED_AT",
    "CI_PIPELINE_NAME",
    "CI_PROJECT_DIR",
    "CI_PROJECT_ID",
    "CI_PROJECT_NAME",
    "CI_PROJECT_NAMESPACE",
    "CI_PROJECT_NAMESPACE_ID",
    "CI_PROJECT_PATH_SLUG",
    "CI_PROJECT_PATH",
    "CI_PROJECT_REPOSITORY_LANGUAGES",
    "CI_PROJECT_ROOT_NAMESPACE",
    "CI_PROJECT_TITLE",
    "CI_PROJECT_DESCRIPTION",
    "CI_PROJECT_URL",
    "CI_PROJECT_VISIBILITY",
    "CI_PROJECT_CLASSIFICATION_LABEL",
    "CI_REGISTRY_IMAGE",
    "CI_REGISTRY_USER",
    "CI_REGISTRY",
    "CI_REPOSITORY_URL",
    "CI_RUNNER_DESCRIPTION",
    "CI_RUNNER_EXECUTABLE_ARCH",
    "CI_RUNNER_ID",
    "CI_RUNNER_REVISION",
    "CI_RUNNER_SHORT_TOKEN",
    "CI_RUNNER_TAGS",
    "CI_RUNNER_VERSION",
    "CI_SERVER_HOST",
    "CI_SERVER_NAME",
    "CI_SERVER_PORT",
    "CI_SERVER_PROTOCOL",
    "CI_SERVER_SHELL_SSH_HOST",
    "CI_SERVER_SHELL_SSH_PORT",
    "CI_SERVER_REVISION",
    "CI_SERVER_URL",
    "CI_SERVER_VERSION_MAJOR",
    "CI_SERVER_VERSION_MINOR",
    "CI_SERVER_VERSION_PATCH",
    "CI_SERVER_VERSION",
    "CI_SERVER",
    "CI_SHARED_ENVIRONMENT",
    "CI_TEMPLATE_REGISTRY_HOST",
    "GITLAB_CI",
    "GITLAB_FEATURES",
    "GITLAB_USER_EMAIL",
    "GITLAB_USER_ID",
    "GITLAB_USER_LOGIN",
    "GITLAB_USER_NAME",
    "KUBECONFIG",
    "TRIGGER_PAYLOAD",
    # BITBUCKET
    "BITBUCKET_BUILD_NUMBER",
    "BITBUCKET_CLONE_DIR",
    "BITBUCKET_COMMIT",
    "BITBUCKET_WORKSPACE",
    "BITBUCKET_REPO_SLUG",
    "BITBUCKET_REPO_UUID",
    "BITBUCKET_REPO_FULL_NAME",
    "BITBUCKET_BRANCH",
    "BITBUCKET_TAG",
    "BITBUCKET_BOOKMARK",
    "BITBUCKET_PARALLEL_STEP",
    "BITBUCKET_PARALLEL_STEP_COUNT",
    "BITBUCKET_PR_ID",
    "BITBUCKET_PR_DESTINATION_BRANCH",
    "BITBUCKET_GIT_HTTP_ORIGIN",
    "BITBUCKET_GIT_SSH_ORIGIN",
    "BITBUCKET_EXIT_CODE",
    "BITBUCKET_STEP_UUID",
    "BITBUCKET_PIPELINE_UUID",
    "BITBUCKET_DEPLOYMENT_ENVIRONMENT",
    "BITBUCKET_DEPLOYMENT_ENVIRONMENT_UUID",
    "BITBUCKET_PROJECT_KEY",
    "BITBUCKET_PROJECT_UUID",
    "BITBUCKET_STEP_TRIGGERER_UUID",
    # CIRCLECI
    "CIRCLECI",
    "CIRCLE_BRANCH",
    "CIRCLE_BUILD_NUM",
    "CIRCLE_BUILD_URL",
    "CIRCLE_JOB",
    "CIRCLE_NODE_INDEX",
    "CIRCLE_NODE_TOTAL",
    "CIRCLE_OIDC_TOKEN",
    "CIRCLE_OIDC_TOKEN_V2",
    "CIRCLE_PR_NUMBER",
    "CIRCLE_PR_REPONAME",
    "CIRCLE_PR_USERNAME",
    "CIRCLE_PREVIOUS_BUILD_NUM",
    "CIRCLE_PROJECT_REPONAME",
    "CIRCLE_PROJECT_USERNAME",
    "CIRCLE_PULL_REQUEST",
    "CIRCLE_PULL_REQUESTS",
    "CIRCLE_REPOSITORY_URL",
    "CIRCLE_SHA1",
    "CIRCLE_TAG",
    "CIRCLE_USERNAME",
    "CIRCLE_WORKFLOW_ID",
    "CIRCLE_WORKFLOW_JOB_ID",
    "CIRCLE_WORKFLOW_WORKSPACE_ID",
    "CIRCLE_WORKING_DIRECTORY",
    "CIRCLE_INTERNAL_TASK_DATA",
    # JENKINS (https://devopsqa.wordpress.com/2019/11/19/list-of-available-jenkins-environment-variables/)
    "BRANCH_NAME",
    "CHANGE_ID",
    "CHANGE_URL",
    "CHANGE_TITLE",
    "CHANGE_AUTHOR",
    "CHANGE_AUTHOR_DISPLAY_NAME",
    "CHANGE_AUTHOR_EMAIL",
    "CHANGE_TARGET",
    "BUILD_NUMBER",
    "BUILD_ID",
    "BUILD_DISPLAY_NAME",
    "JOB_NAME",
    "JOB_BASE_NAME",
    "BUILD_TAG",
    "EXECUTOR_NUMBER",
    "NODE_NAME",
    "NODE_LABELS",
    "WORKSPACE",
    "JENKINS_HOME",
    "JENKINS_URL",
    "BUILD_URL",
    "JOB_URL",
    "GIT_COMMIT",
    "GIT_PREVIOUS_COMMIT",
    "GIT_PREVIOUS_SUCCESSFUL_COMMIT",
    "GIT_BRANCH",
    "GIT_LOCAL_BRANCH",
    "GIT_URL",
    "GIT_COMMITTER_NAME",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_AUTHOR_EMAIL",
    "SVN_REVISION",
    "SVN_URL",
    # TRAVIS CI
    "TRAVIS_ALLOW_FAILURE",
    "TRAVIS_APP_HOST",
    "TRAVIS_BRANCH",
    "TRAVIS_TAG",
    "TRAVIS_BUILD_DIR",
    "TRAVIS_BUILD_ID",
    "TRAVIS_BUILD_NUMBER",
    "TRAVIS_BUILD_WEB_URL",
    "TRAVIS_COMMIT",
    "TRAVIS_COMMIT_MESSAGE",
    "TRAVIS_COMMIT_RANGE",
    "TRAVIS_COMPILER",
    "TRAVIS_DEBUG_MODE",
    "TRAVIS_DIST",
    "TRAVIS_EVENT_TYPE",
    "TRAVIS_JOB_ID",
    "TRAVIS_JOB_NAME",
    "TRAVIS_JOB_NUMBER",
    "TRAVIS_JOB_WEB_URL",
    "TRAVIS_OS_NAME",
    "TRAVIS_CPU_ARCH",
    "TRAVIS_OSX_IMAGE",
    "TRAVIS_PULL_REQUEST",
    "TRAVIS_PULL_REQUEST_BRANCH",
    "TRAVIS_PULL_REQUEST_SHA",
    "TRAVIS_PULL_REQUEST_SLUG",
    "TRAVIS_REPO_SLUG",
    "TRAVIS_SECURE_ENV_VARS",
    "TRAVIS_SUDO",
    "TRAVIS_TEST_RESULT",
    "TRAVIS_TAG",
    "TRAVIS_BUILD_STAGE_NAME",
    "TRAVIS_DART_VERSION",
    "TRAVIS_GO_VERSION",
    "TRAVIS_HAXE_VERSION",
    "TRAVIS_JDK_VERSION",
    "TRAVIS_JULIA_VERSION",
    "TRAVIS_NODE_VERSION",
    "TRAVIS_OTP_RELEASE",
    "TRAVIS_PERL_VERSION",
    "TRAVIS_PHP_VERSION",
    "TRAVIS_PYTHON_VERSION",
    "TRAVIS_R_VERSION",
    "TRAVIS_RUBY_VERSION",
    "TRAVIS_RUST_VERSION",
    "TRAVIS_SCALA_VERSION",
    "TRAVIS_MARIADB_VERSION",
    "TRAVIS_XCODE_SDK",
    "TRAVIS_XCODE_SCHEME",
    "TRAVIS_XCODE_PROJECT",
    "TRAVIS_XCODE_WORKSPACE",
]
