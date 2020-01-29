import amqp from 'amqplib'

export interface TaskQueueServiceParams {
  url: string
  queue: string
}

class TaskQueueService {
  private params: TaskQueueServiceParams

  private connection?: amqp.Connection
  private channel?: amqp.Channel

  public constructor(params: TaskQueueServiceParams) {
    this.params = params
  }

  public async connect() {
    const { url, queue } = this.params
    this.connection = await amqp.connect(url)
    this.channel = await this.connection?.createChannel()
    this.channel.assertQueue(queue, { durable: false })
  }

  public async send() {
    const { url, queue } = this.params

    if (!this.channel) {
      throw new Error(
        `TaskQueueService: attempted to send a message to a channel that is not connected. Server url: '${url}', queue: '${queue}'`,
      )
    }

    const msg = 'Hello World!'
    this.channel.sendToQueue(queue, Buffer.from(msg), { persistent: true })
    console.log(' [x] Sent %s', msg)
  }

  public close = () => {
    this.connection?.close()
  }
}

export default TaskQueueService
