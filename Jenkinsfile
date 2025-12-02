pipeline {
    agent any


    environment {
        REPO_URL = 'https://github.com/funmicra/Docker-Update.git'
        BRANCH = 'master'
        COMPOSE_PROJECT_NAME = 'Docker-Update'
    }

    stages {
        stage('Checkout') {
            steps {
                echo "Checking out GitHub repository..."
                git branch: "${BRANCH}", url: "${REPO_URL}"
            }
        }

        stage('Build Docker Images') {
            steps {
                echo "Building Docker images..."
                sh 'docker-compose -p ${COMPOSE_PROJECT_NAME} build'
            }
        }

        stage('Deploy to Remote Host') {
            steps {
                sshagent(['${SSH_KEY_ID}']) { 
                    sh """
                    ssh -o StrictHostKeyChecking=no funmicra@192.168.88.22 '
                        cd /path/to/remote/project &&
                        docker-compose -p ${COMPOSE_PROJECT_NAME} pull &&
                        docker-compose -p ${COMPOSE_PROJECT_NAME} up -d
                    '
                    """
                }
            }
        }
    
    }

    post {
        success {
            echo "Deployment completed successfully!"
        }
        failure {
            echo "Deployment failed. Check the logs."
        }
    }
}
